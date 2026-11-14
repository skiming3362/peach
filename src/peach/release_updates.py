"""公开测试版查询；传输复用共享 HTTPX，发行包从自己的 fork 的 GitHub Releases 取。

这份检出是本机自用的修改版，所以更新源指向自己的 fork，不是上游 `peach-mitao/peach`：
上游发新版时本机不该被它带走，只有 fork 上挂出带制品的发布才会被检查到。制品由
`scripts/package_windows_release.py` 打，`state/project-context/publish-release.ps1` 负责
挂到 fork 的 release 上。

上游另有 `downloads.peach.video` 这个镜像源。fork 没有对应服务，所以它不参与发行列表：
列表里挂的是上游的包，一旦参与判断，上游发的新版就会提示本机升级，等于把本机交回上游。
镜像只留作**同一版本、同一文件名**的下载备份（`download_sources`），下载时两个源的字节都要
对上发布条目给的摘要。
"""
from __future__ import annotations

import json
import platform
import re
import sys

import httpx

from . import __version__, distribution, docker_update
from .http import HttpRequest, HttpxTransport
from .update_packages import TOOL_PATHS
from . import interface_text
from .user_messages import UserValueError, message_fields

#: 更新源的 GitHub 归属。改这一处等于换发布通道：检查、下载地址和发布页链接都跟着变。
REPOSITORY = "skiming3362/peach"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
#: 上游的公开镜像。fork 没有对应服务，只作同版本下载备份，不参与发行列表。
DOWNLOADS_BASE = "https://downloads.peach.video"
LISTING_RULE = 'source-aware-v1'
#: 安装方式与更新通道：响应带 `installation_code`、`channel_code`，前端按它取译文和判断；
#: 同层的名字按 zh-CN 渲染（`server:release.installation.<id>`、`server:release.channel.<id>`）。
INSTALLATIONS = ("docker", "standalone", "source")
CHANNELS = ("test",)
#: 检查结果的一句话，`message_code` 是 `release.message.<键>`。
MESSAGES = ("unchecked", "empty", "error", "staleListing", "available", "ahead", "current", "imagePending",
            "imageAvailable")


def _message(key: str, **params) -> dict:
    return message_fields(f"release.message.{key}", params)


def download_sources(url: str) -> tuple[str, ...]:
    """发行包保留同版本、同文件名的另一公开源；校验沿用原发布摘要。

    主源一律是本 fork 的发布地址（`_release_candidates` 只按它拼地址），备份是上游镜像上
    同版本同文件名的那个文件。反过来也认：手动粘进来的镜像地址能退回发布页。镜像上没有
    这个版本时备份返回 404，主源的校验失败仍然如实报出来（`standalone_update.download`）。
    """
    github = RELEASES_URL + "/download/"
    mirror = DOWNLOADS_BASE + "/"
    for primary, backup in ((github, mirror), (mirror, github)):
        if url.startswith(primary):
            return url, backup + url.removeprefix(primary)
    return (url,)


#: 容器换新版在宿主机上做：拉镜像、重建容器，`/data` 卷保留。每步一个 id、一句说明加一条命令，
#: 命令按仓库的 compose.yaml 写（镜像标签取 `.env` 的 `PEACH_VERSION`，缺省是 `edge`）。
#: 说明在 `server:release.dockerStep.<id>`。
DOCKER_STEPS = (("pull", "docker compose pull"), ("recreate", "docker compose up -d"))


def snapshot() -> dict:
    docker = distribution.docker()
    installation = "docker" if docker else "standalone" if distribution.standalone() else "source"
    return {"current_version": __version__, "latest_version": None,
            "channel": interface_text.text("server:release.channel.test"), "channel_code": "test",
            "installation": interface_text.text(f"server:release.installation.{installation}"),
            "installation_code": installation,
            "in_app_update": distribution.in_app_update(),
            "steps": [{"id": step, "text": interface_text.text(f"server:release.dockerStep.{step}"), "command": command}
                      for step, command in DOCKER_STEPS]
            if docker else [],
            "state": "unchecked", **_message("unchecked"), "release_url": RELEASES_URL}


def _version(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        return None
    return tuple(map(int, value.removeprefix("v").split(".")))


def _release_candidates(rows: list, system: str = "win32", machine: str = "") -> list[tuple]:
    """从发行的发布列表选择本机平台的完整包，并构造受信任的下载地址。

    macOS 选与本机架构同名的 DMG（`build_macos_standalone.py` 按 `platform.machine()` 命名），
    没有分包与工具包；其余平台沿用 Windows 包判断有无新版本。下载地址一律由这里按 tag 和
    资产名拼出来，不采信发行条目里的 `browser_download_url`——那是发布者可以随手填的字段，
    摘要才是校验依据。
    """
    releases = []
    for row in rows:
        if not isinstance(row, dict) or row.get("draft"):
            continue
        version = _version(row.get("tag_name"))
        if version is None:
            continue
        tag = row["tag_name"]
        assets = row.get("assets") or []
        uploaded = {a.get("name"): a for a in assets if isinstance(a, dict) and a.get("state") == "uploaded"}
        if system == "darwin":
            full = asset = uploaded.get(f"Peach-{tag.removeprefix('v')}-macos-{machine}.dmg")
            if full is None:
                continue
        else:
            prefix = f"Peach-{tag.removeprefix('v')}-windows-x64"
            full = uploaded.get(prefix + ".zip")
            if full is None:
                continue
            complete_tools = all(prefix + f"-tools-{tool}.zip" in uploaded for tool in TOOL_PATHS)
            asset = uploaded.get(prefix + "-update.zip", full) if complete_tools else full
        directory = f"{RELEASES_URL}/download/{tag}"
        release_url = f"{RELEASES_URL}/tag/{tag}"
        releases.append((version, tag, asset, full, directory, release_url))
    return releases


def _listing(client) -> list | None:
    """发行列表；取不到时给 None，让调用方区分「没查成」和「查成了但没有可下载的版本」。"""
    try:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        response = client(HttpRequest("GET", API_URL, headers), 12.0, 2_000_000)
        if response.status != 200:
            return None
        rows = json.loads(response.body)
    except (OSError, ValueError, TypeError, TimeoutError, httpx.HTTPError):
        return None
    return rows if isinstance(rows, list) else None


def tool_package(tool: str, *, transport=None) -> dict:
    """最新公开版本里的 Windows 工具包 `Peach-<版本>-windows-x64-tools-<tool>.zip`。

    源码运行的版本号不一定有同名发布（主线可能已在下一版），所以不按本机版本拼地址，
    取列表里最新的那份；工具包与程序版本无关，包内 `manifest.json` 自带工具版本。
    返回 `url`、`size`、`digest`（`sha256:` 前缀）与 `version`；取不到时抛 ValueError。
    """
    client = transport or HttpxTransport()
    try:
        rows = _listing(client) or []
        found = []
        for row in rows:
            version = _version(row.get("tag_name")) if isinstance(row, dict) and not row.get("draft") else None
            if version is None:
                continue
            tag = row["tag_name"]
            name = f"Peach-{tag.removeprefix('v')}-windows-x64-tools-{tool}.zip"
            asset = next((a for a in row.get("assets") or [] if isinstance(a, dict)
                          and a.get("name") == name and a.get("state") == "uploaded"), None)
            if (asset is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or "")
                    or not isinstance(asset.get("size"), int)):
                continue
            found.append((version, {"url": f"{RELEASES_URL}/download/{tag}/{name}", "size": asset["size"],
                                    "digest": asset["digest"], "version": tag.removeprefix("v")}))
        if not found:
            raise UserValueError("errors.releaseUpdates.toolPackageNotFound")
        return max(found, key=lambda item: item[0])[1]
    finally:
        if transport is None:
            client.close()


def check(*, transport=None, system: str | None = None, machine: str | None = None) -> dict:
    result = snapshot()
    system = system or sys.platform
    machine = machine or platform.machine()
    client = transport or HttpxTransport()
    try:
        rows = _listing(client)
        if rows is None:
            return dict(result, state="error", **_message("error"))
        releases = _release_candidates(rows, system, machine)
        # 列表取到了却没有可下载的版本，是「查成了但没东西」，与上面「没查成」分开报。
        if not releases:
            return dict(result, state="empty", **_message("empty"))
        version, tag, asset, full, directory, release_url = max(releases, key=lambda item: item[0])
        current = _version(result["current_version"])
        if current is None:
            raise ValueError("current version required")
        state = "available" if version > current else "ahead" if version < current else "current"
        message = _message(state)
        if distribution.docker():
            # 版本号跟着 Windows 包判；镜像在 Release 之后才推送，空窗里拉到的还是旧镜像，
            # 所以以 Docker Hub 上版本号标签与 edge 指向同一镜像为准（ADR-0140）。
            latest = tag.removeprefix("v")
            if state == "available" and not docker_update.published(client, latest):
                state, message = "pending", _message("imagePending", version=latest)
            extra = {"one_click": docker_update.status()} if state == "available" else {}
            return dict(result, latest_version=latest, state=state, release_url=release_url, **extra,
                        **(_message("imageAvailable") if state == "available" else message))
        return dict(result, latest_version=tag.removeprefix("v"), state=state, **message,
                    asset_url=f"{directory}/{asset['name']}",
                    asset_size=asset.get("size", 0), asset_digest=asset.get("digest"),
                    package_kind="installer" if system == "darwin" else "update" if asset is not full else "full",
                    # 应用更新包准备不下去（清单或工具包与本版更新器对不上）时改下的同版本完整包。
                    full_url=f"{directory}/{full['name']}", full_size=full.get("size", 0),
                    full_digest=full.get("digest"),
                    release_url=release_url)
    except (OSError, ValueError, TypeError, TimeoutError, httpx.HTTPError):
        return dict(result, state="error", **_message("error"))
    finally:
        if transport is None:
            client.close()
