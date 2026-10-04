// Ohmz fork: the parts of the old Live tab that the home screen does not already show. The metric
// tiles (CPU, memory, swap, GPU, load, disk, network) are all charts on the home screen already, so
// they are deliberately not repeated here. This is rendered under the system charts on the home page.
import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { bytes, dotFor, dur, useMaintenanceFile, when } from "@/lib/maintenance"

type Top = { name?: string; class?: string; cpu_pct?: number; mem_gib?: number }
type Holder = { who?: string; kind?: string; swap_b?: number; resident_b?: number }
type Live = {
	generated_at?: number
	interval_s?: number
	host?: { cores?: number; uptime_s?: number; psi?: Record<string, number> }
	containers?: { running?: number; top_cpu?: Top[]; top_mem?: Top[]; unhealthy?: string[] }
	services?: { name?: string; state?: string; detail?: string }[]
	io_top?: { readers?: { name?: string; container?: string | null; read_bps?: number; write_bps?: number }[] }
	swap?: { used_pct?: number; state?: string; in_bps?: number; out_bps?: number; holders?: Holder[] }
	pressure?: { level?: number; level_name?: string; why?: string }
	self?: { tick_ms?: number; rss_mb?: number; ticks?: number; errors?: number }
}

const num = (n: number | undefined, d = 1) => (typeof n === "number" && Number.isFinite(n) ? n.toFixed(d) : "-")
const rate = (bps?: number) => (typeof bps === "number" ? `${bytes(bps)}/s` : "-")
const lvTone = (n = 0) => (n >= 4 ? "crit" : n >= 2 ? "warn" : n >= 1 ? "info" : "ok")

export const LivePanels = memo(() => {
	const d = useMaintenanceFile<Live>("live.json")
	if (!d) return null
	const psi = d.host?.psi ?? {}
	const lvl = d.pressure?.level ?? 0
	const swap = d.swap ?? {}

	return (
		<>
			<div className="grid gap-3 md:grid-cols-2 mb-6 mt-6">
				<div className="rounded-lg border border-border bg-card p-4">
					<div className="flex items-center gap-2 mb-2">
						<span className={`block size-2.5 rounded-full ${dotFor(lvTone(lvl))}`} />
						<span className="text-sm font-semibold">
							<Trans>Pressure</Trans> · {lvl} {d.pressure?.level_name ? `(${d.pressure.level_name})` : ""}
						</span>
						{d.generated_at ? (
							<span className="ms-auto text-xs text-muted-foreground">
								<Trans>updated</Trans> {when(d.generated_at)}
							</span>
						) : null}
					</div>
					{d.pressure?.why ? <p className="text-sm text-muted-foreground mb-3">{d.pressure.why}</p> : null}
					<div className="grid grid-cols-3 gap-3 text-sm">
						{[
							["Memory stall", psi.mem_some60, psi.mem_full60],
							["I/O stall", psi.io_some60, psi.io_full60],
							["CPU wait", psi.cpu_some60, undefined],
						].map(([label, some, full]) => (
							<div key={label as string}>
								<div className="text-xs text-muted-foreground">{label as string}</div>
								<div className="tabular-nums">
									some {num(some as number)}%{typeof full === "number" ? ` · full ${num(full)}%` : ""}
								</div>
							</div>
						))}
					</div>
				</div>

				<div className="rounded-lg border border-border bg-card p-4">
					<div className="flex items-center gap-2 mb-2">
						<span className="text-sm font-semibold">
							<Trans>Swap held by</Trans>
						</span>
						<span className="ms-auto text-xs text-muted-foreground">
							{num(swap.used_pct, 0)}% · {swap.state ?? ""} · in {rate(swap.in_bps)} / out {rate(swap.out_bps)}
						</span>
					</div>
					<ul className="text-sm">
						{(swap.holders ?? []).slice(0, 6).map((x, i) => (
							<li key={i} className="flex items-center gap-2 py-0.5 border-t border-border/40">
								<span className="text-xs text-muted-foreground w-16">{x.kind ?? ""}</span>
								<span className="truncate">{x.who}</span>
								<span className="ms-auto tabular-nums">{bytes(x.swap_b)}</span>
							</li>
						))}
						{!swap.holders?.length ? <li className="text-muted-foreground py-0.5">No swap holders.</li> : null}
					</ul>
				</div>
			</div>

			{d.containers?.unhealthy?.length ? (
				<div className="rounded-lg border border-yellow-500/40 bg-yellow-500/5 px-4 py-3 text-sm mb-6">
					<span className="font-medium">
						<Trans>Unhealthy</Trans>:
					</span>{" "}
					{d.containers.unhealthy.join(", ")}
				</div>
			) : null}

			<div className="grid gap-4 md:grid-cols-2 mb-6">
				<div>
					<h2 className="text-lg font-semibold mb-2">
						<Trans>Busiest by CPU</Trans>
					</h2>
					<ul className="text-sm">
						{(d.containers?.top_cpu ?? []).slice(0, 6).map((c) => (
							<li key={c.name} className="flex items-center gap-2 py-1 border-t border-border/40">
								<span className="text-xs text-muted-foreground w-7">{c.class ?? ""}</span>
								<span className="truncate">{c.name}</span>
								<span className="ms-auto tabular-nums">{num(c.cpu_pct)}%</span>
							</li>
						))}
					</ul>
				</div>
				<div>
					<h2 className="text-lg font-semibold mb-2">
						<Trans>Largest by memory</Trans>
					</h2>
					<ul className="text-sm">
						{(d.containers?.top_mem ?? []).slice(0, 6).map((c) => (
							<li key={c.name} className="flex items-center gap-2 py-1 border-t border-border/40">
								<span className="text-xs text-muted-foreground w-7">{c.class ?? ""}</span>
								<span className="truncate">{c.name}</span>
								<span className="ms-auto tabular-nums">{num(c.mem_gib, 2)} GiB</span>
							</li>
						))}
					</ul>
				</div>
			</div>

			{(d.services?.length ?? 0) > 0 ? (
				<>
					<h2 className="text-lg font-semibold mb-2">
						<Trans>Services</Trans>
					</h2>
					<div className="grid gap-x-4 sm:grid-cols-2 lg:grid-cols-3 mb-6 text-sm">
						{(d.services ?? []).map((s) => (
							<div key={s.name} className="flex items-center gap-2 py-1 border-t border-border/40">
								<span className={`block size-2 rounded-full ${dotFor(s.state === "up" ? "ok" : (s.state ?? ""))}`} />
								<span className="truncate">{s.name}</span>
								<span className="ms-auto text-muted-foreground truncate">{s.detail ?? ""}</span>
							</div>
						))}
					</div>
				</>
			) : null}

			{(d.io_top?.readers?.length ?? 0) > 0 ? (
				<>
					<h2 className="text-lg font-semibold mb-2">
						<Trans>Disk activity</Trans>
					</h2>
					<ul className="text-sm mb-6">
						<li className="flex items-center gap-2 py-1 border-t border-border/40 text-xs uppercase tracking-wide text-muted-foreground">
							<span className="flex-1">
								<Trans>Process</Trans>
							</span>
							<span className="w-40">
								<Trans>Container</Trans>
							</span>
							<span className="w-28 text-right">
								<Trans>Read</Trans>
							</span>
							<span className="w-28 text-right">
								<Trans>Write</Trans>
							</span>
						</li>
						{(d.io_top?.readers ?? []).slice(0, 8).map((r, i) => (
							<li key={i} className="flex items-center gap-2 py-1 border-t border-border/40">
								<span className="flex-1 font-mono text-xs truncate">{r.name}</span>
								<span className="w-40 text-muted-foreground text-xs truncate">{r.container ?? "—"}</span>
								<span className="w-28 text-right tabular-nums">{rate(r.read_bps)}</span>
								<span className="w-28 text-right tabular-nums">{rate(r.write_bps)}</span>
							</li>
						))}
					</ul>
				</>
			) : null}

			{d.self ? (
				<p className="text-xs text-muted-foreground mb-2">
					<Trans>Live monitor</Trans> · tick {num(d.self.tick_ms)} ms · {num(d.self.rss_mb, 1)} MB · {d.self.ticks ?? 0}{" "}
					<Trans>samples</Trans> · {d.self.errors ?? 0} <Trans>errors</Trans> · up {dur(d.host?.uptime_s)}
				</p>
			) : null}
		</>
	)
})
