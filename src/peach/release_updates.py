"""公开测试版查询；传输复用共享 HTTPX，发行包从 GitHub Releases 取。

这份检出发的是本机自用的修改版，所以更新源指向自己的 fork，不是上游
`peach-mitao/peach`：上游发新版时本机不该被它带走，只有 fork 上挂出带制品的发布才会被
检查到。制品由 `scripts/package_windows_release.py` 打，`state/project-context/
publish-release.ps1` 负责挂到 fork 的 release 上。上游另有 `downloads.peach.video` 这个
镜像源，fork 没有对应服务，这里不再保留第二个源。
"""
from __future__ import annotations

import json
import re
import httpx

from . import __version__, distribution
from .http import HttpRequest, HttpxTransport
from .update_packages import TOOL_PATHS

#: 更新源的 GitHub 归属。改这一处等于换发布通道：检查、下载地址和发布页链接都跟着变。
REPOSITORY = "skiming3362/peach"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"


def download_sources(url: str) -> tuple[str, ...]:
    """发行包的下载地址。只剩 GitHub 一个来源，保留成列表是为了让调用方不必改形状。"""
    return (url,)


def snapshot() -> dict:
    return {"current_version": __version__, "latest_version": None,
            "channel": "测试版", "installation": "独立测试包" if distribution.standalone() else "源码运行",
            "state": "unchecked", "message": "尚未检查", "release_url": RELEASES_URL}


def _version(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        return None
    return tuple(map(int, value.removeprefix("v").split(".")))


def _release_candidates(rows: list) -> list[tuple]:
    """从 GitHub 的发行列表里挑出完整包，并构造受信任的下载地址。

    只认带全套工具包的那一版更新包：缺工具包时只下完整包，靠它补齐工具。下载地址一律
    由这里按 tag 和资产名拼出来，不采信发行条目里的 `browser_download_url`——那是发布者
    可以随手填的字段，摘要才是校验依据。
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
        prefix = f"Peach-{tag.removeprefix('v')}-windows-x64"
        uploaded = {a.get("name"): a for a in assets if isinstance(a, dict) and a.get("state") == "uploaded"}
        full = uploaded.get(prefix + ".zip")
        if full is None:
            continue
        complete_tools = all(prefix + f"-tools-{tool}.zip" in uploaded for tool in TOOL_PATHS)
        asset = uploaded.get(prefix + "-update.zip", full) if complete_tools else full
        directory = f"{RELEASES_URL}/download/{tag}"
        releases.append((version, tag, asset, full, directory, f"{RELEASES_URL}/tag/{tag}"))
    return releases


def check(*, transport=None) -> dict:
    result = snapshot()
    client = transport or HttpxTransport()
    try:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        response = client(HttpRequest("GET", API_URL, headers), 12.0, 2_000_000)
        rows = json.loads(response.body) if response.status == 200 else None
        releases = _release_candidates(rows) if isinstance(rows, list) else []
        if not releases:
            return dict(result, state="empty" if isinstance(rows, list) else "error",
                        message="暂未找到可下载的测试版。" if isinstance(rows, list) else "未能取得版本信息，请重试。")
        version, tag, asset, full, directory, release_url = max(releases, key=lambda item: item[0])
        current = _version(result["current_version"])
        if current is None:
            raise ValueError("current version required")
        state = "available" if version > current else "ahead" if version < current else "current"
        message = {"available": "有新版本可下载。", "ahead": "当前版本高于已发布测试版。", "current": "已是最新测试版。"}[state]
        return dict(result, latest_version=tag.removeprefix("v"), state=state, message=message,
                    asset_url=f"{directory}/{asset['name']}",
                    asset_size=asset.get("size", 0), asset_digest=asset.get("digest"),
                    package_kind="update" if asset is not full else "full",
                    # 应用更新包准备不下去（清单或工具包与本版更新器对不上）时改下的同版本完整包。
                    full_url=f"{directory}/{full['name']}", full_size=full.get("size", 0),
                    full_digest=full.get("digest"),
                    release_url=release_url)
    except (OSError, ValueError, TypeError, TimeoutError, httpx.HTTPError):
        return dict(result, state="error", message="未能取得版本信息，请重试。")
    finally:
        if transport is None:
            client.close()
