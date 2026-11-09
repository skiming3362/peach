(function() {
	try {
		var e = typeof globalThis < "u" ? globalThis : typeof window < "u" ? window : typeof global < "u" ? global : typeof self < "u" ? self : {}, t = new e.Error().stack;
		t && (e._sentryDebugIds = e._sentryDebugIds || {}, e._sentryDebugIds[t] = "0761e4f9-486b-4bef-96ab-236ad3b153b1", e._sentryDebugIdIdentifier = "sentry-dbid-0761e4f9-486b-4bef-96ab-236ad3b153b1");
	} catch {}
})();
import { Button as e, LinkButton as t, Note as n, Progress as r, REPAIRS_KEY as i, RESULT_PANEL as a, RESULT_SECTION as o, SectionHeading as s, StatusLabel as c, apiSend as l, confirmModal as u, fetchRepairs as d, fmtSize as f, i2 as p, interfaceLanguage as m, queryClient as h, repairCenterPanelSkeleton as g, require_jsx_runtime as _, t as v, useAction as y, useQuery as b } from "./peach-app.js";
import { Page as x } from "./peach-app.page-QAncjzM1.js";
// src/react/settings/repair-center.tsx
var S = _(), C = {
	done: [v("settings:shared.completed"), "success"],
	succeeded: [v("settings:shared.completed"), "success"],
	complete: [v("settings:shared.completed"), "success"],
	running: [v("settings:shared.inProgress"), "progress"],
	pending: [v("settings:repairCenter.queued"), "neutral"],
	waiting: [v("settings:repairCenter.awaitingConfirmation"), "warning"],
	failed: [v("settings:repairCenter.needsRetry"), "error"],
	none: [v("settings:repairCenter.nothingToDo"), "neutral"]
}, w = (e = 0) => e.toLocaleString(), T = (e) => (/* @__PURE__ */ new Date(e * 1e3)).toLocaleString(m, {
	year: "numeric",
	month: "2-digit",
	day: "2-digit",
	hour: "2-digit",
	minute: "2-digit"
}), E = (e) => v("settings:repairCenter.filesAndSize", {
	count: e.files || 0,
	size: f(e.bytes || 0)
}), D = (e) => e.state === "running" && !!e.total, O = (e) => !!e.estimating || e.state === "running";
function k(e) {
	return e.estimating ? v("settings:repairCenter.estimating") : e.state === "failed" && e.error ? e.error : e.files ? v("settings:repairCenter.reclaimable", { size: f(e.bytes || 0) }) : e.state === "waiting" ? e.pending ? v("settings:repairCenter.pendingRecheck", { count: e.pending }) : "" : D(e) ? "" : e.total ? `${w(e.done)} / ${w(e.total)}` : e.mode === "confirm" ? "" : e.changed ? v("settings:repairCenter.changedCount", { count: e.changed }) : "";
}
function A({ title: e, description: t, progress: n, children: r }) {
	return /* @__PURE__ */ (0, S.jsxs)("div", {
		className: `${o} flex flex-wrap items-center justify-between gap-x-4 gap-y-2`,
		children: [/* @__PURE__ */ (0, S.jsxs)("div", {
			className: "flex min-w-0 flex-1 flex-col gap-0.5",
			children: [
				/* @__PURE__ */ (0, S.jsx)("span", {
					className: "text-body-medium text-text-primary",
					children: e
				}),
				/* @__PURE__ */ (0, S.jsx)("span", {
					className: "text-body-2-regular text-text-secondary",
					children: t
				}),
				n ? /* @__PURE__ */ (0, S.jsx)("div", {
					className: "pt-2",
					children: n
				}) : null
			]
		}), /* @__PURE__ */ (0, S.jsx)("div", {
			className: "flex shrink-0 items-center gap-3",
			children: r
		})]
	});
}
function j({ item: e }) {
	return /* @__PURE__ */ (0, S.jsxs)("div", {
		className: "flex items-center gap-3",
		children: [/* @__PURE__ */ (0, S.jsx)("div", {
			className: "min-w-0 flex-1",
			children: /* @__PURE__ */ (0, S.jsx)(r, {
				label: v("settings:repairCenter.itemProgress", { title: e.title }),
				value: e.done || 0,
				max: e.total
			})
		}), /* @__PURE__ */ (0, S.jsxs)("span", {
			className: "shrink-0 text-caption-1-regular text-text-secondary tabular-nums",
			children: [
				w(e.done),
				" / ",
				w(e.total)
			]
		})]
	});
}
function M() {
	let r = b({
		queryKey: i,
		queryFn: ({ signal: e }) => d(e),
		refetchInterval: ({ state: e }) => e.data?.items.some(O) ? 2e3 : !1
	}), o = r.data, f = y(), m = (e) => h.setQueryData(i, e), _ = (e, t) => l(`/api/configuration/repairs/${e.id}`, {}, "POST", t), w = (e) => e.files === void 0 ? void f.run(e.id, (t) => _(e, t), m) : void u({
		title: v("settings:repairCenter.deleteTitle", { title: e.title }),
		body: v("settings:repairCenter.deleteBody", { amount: E(e) }),
		confirmLabel: v("settings:repairCenter.deleteSnapshots"),
		danger: !0,
		onConfirm: async () => m(await _(e))
	}), M = (e) => e.backups.length ? e.backups.some((e) => e.exists) ? /* @__PURE__ */ (0, S.jsx)(t, {
		href: "/settings/maintenance#ledgerBackups",
		size: "small",
		trailingIcon: p,
		children: v("settings:repairCenter.preRepairBackup")
	}) : /* @__PURE__ */ (0, S.jsx)("span", {
		className: "text-body-2-regular text-text-tertiary",
		children: v("settings:repairCenter.backupCleaned")
	}) : null, N = f.error || (r.error ? r.error.message : "");
	return /* @__PURE__ */ (0, S.jsxs)(x, { children: [
		N ? /* @__PURE__ */ (0, S.jsx)(n, {
			tone: "error",
			children: N
		}) : null,
		/* @__PURE__ */ (0, S.jsxs)("section", {
			"aria-labelledby": "repair-items",
			className: "flex flex-col gap-4",
			children: [/* @__PURE__ */ (0, S.jsx)(s, {
				id: "repair-items",
				children: v("settings:repairCenter.itemsHeading")
			}), o ? o.items.length ? /* @__PURE__ */ (0, S.jsx)("div", {
				className: a,
				children: o.items.map((t) => {
					let [n, r] = C[t.state] || C.none, i = k(t);
					return /* @__PURE__ */ (0, S.jsxs)(A, {
						title: t.title,
						description: t.summary,
						progress: D(t) ? /* @__PURE__ */ (0, S.jsx)(j, { item: t }) : null,
						children: [i ? /* @__PURE__ */ (0, S.jsx)("span", {
							className: "text-body-2-regular text-text-secondary",
							children: i
						}) : null, t.mode === "confirm" && t.state === "waiting" ? /* @__PURE__ */ (0, S.jsx)(e, {
							size: "small",
							disabled: !o.available || !!f.busy || t.estimating,
							onClick: () => w(t),
							children: v("settings:shared.start")
						}) : /* @__PURE__ */ (0, S.jsx)(c, {
							tone: r,
							children: n
						})]
					}, t.id);
				})
			}) : /* @__PURE__ */ (0, S.jsx)("p", {
				className: "text-body-2-regular text-text-secondary",
				children: v("settings:repairCenter.empty")
			}) : /* @__PURE__ */ (0, S.jsx)("div", {
				className: "contents",
				"data-skeleton": "",
				"aria-busy": "true",
				"aria-label": v("settings:repairCenter.loadingItems"),
				dangerouslySetInnerHTML: { __html: g() }
			})]
		}),
		o?.history.length ? /* @__PURE__ */ (0, S.jsxs)("section", {
			"aria-labelledby": "repair-history",
			className: "flex flex-col gap-4",
			children: [/* @__PURE__ */ (0, S.jsx)(s, {
				id: "repair-history",
				children: v("settings:repairCenter.history")
			}), /* @__PURE__ */ (0, S.jsx)("div", {
				className: a,
				children: o.history.map((e) => /* @__PURE__ */ (0, S.jsx)(A, {
					title: e.title,
					description: `${T(e.at)} · ${e.changed ? v("settings:repairCenter.changedCount", { count: e.changed }) : v("settings:repairCenter.noChanges")}`,
					children: M(e)
				}, e.id))
			})]
		}) : null
	] });
}
export { M as RepairCenterPage };

//# debugId=0761e4f9-486b-4bef-96ab-236ad3b153b1