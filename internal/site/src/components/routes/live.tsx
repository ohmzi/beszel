import { t } from "@lingui/core/macro"
import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Sparkline } from "@/components/sparkline"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { bytes, dotFor, dur, useMaintenanceFile, when } from "@/lib/maintenance"

type Mem = { total?: number; used?: number; avail?: number; cache?: number; swap_used?: number; swap_total?: number }
type Disk = { mount?: string; free_b?: number; size_b?: number; used_pct?: number }
type Io = { dev?: string; read_bps?: number; write_bps?: number; util_pct?: number }
type Top = { name?: string; class?: string; cpu_pct?: number; mem_gib?: number }
type Holder = { who?: string; kind?: string; swap_b?: number; resident_b?: number; cap_b?: number | null }

type Live = {
	generated_at?: number
	interval_s?: number
	host?: {
		uptime_s?: number
		cores?: number
		cpu_pct?: number
		cpu_user?: number
		cpu_sys?: number
		cpu_iowait?: number
		load?: number[]
		mem?: Mem
		psi?: Record<string, number>
		disk?: Disk[]
		io?: Io[]
		net?: { rx_bps?: number; tx_bps?: number }
	}
	gpu?: {
		util?: number
		mem_used?: number
		mem_total?: number
		temp?: number
		power_w?: number
		fan_pct?: number
		stale?: boolean
	}
	sensors?: {
		cpu_temp?: number
		gpu_temp?: number
		ram_temp?: number
		nvme_temp?: number
		cpu_fan_rpm?: number
		case_fan_rpm?: number
		stale?: boolean
	}
	containers?: { running?: number; stale?: boolean; top_cpu?: Top[]; top_mem?: Top[]; unhealthy?: string[] }
	services?: { name?: string; state?: string; detail?: string; ms?: number; stale?: boolean }[]
	io_top?: {
		readers?: { name?: string; container?: string | null; orphan?: boolean; read_bps?: number; write_bps?: number }[]
		stale?: boolean
	}
	swap?: {
		used_b?: number
		total_b?: number
		used_pct?: number
		state?: string
		in_bps?: number
		out_bps?: number
		exhausted?: boolean
		holders?: Holder[]
	}
	activity?: {
		maintenance_running?: string[]
		check_running?: boolean
		last_action?: { ts?: number; task?: string; action?: string }
	}
	pressure?: { age_s?: number; gate_level?: number; level?: number; level_name?: string; why?: string }
	probes?: Record<string, { age_s?: number; stale?: boolean }>
	self?: {
		pid?: number
		started_at?: number
		ticks?: number
		errors?: number
		tick_ms?: number
		rss_mb?: number
		threads?: number
	}
	history?: {
		cpu?: number[]
		mem_pct?: number[]
		swap_pct?: number[]
		vram_pct?: number[]
		gpu?: number[]
		load1?: number[]
		net_rx?: number[]
		net_tx?: number[]
		disk_r?: number[]
		disk_w?: number[]
	}
}

const clamp = (n: number) => Math.max(0, Math.min(100, n))
const tone = (n: number | undefined, warn: number, crit: number) =>
	n == null ? "" : n >= crit ? "crit" : n >= warn ? "warn" : "ok"
const lvTone = (n = 0) => (n >= 4 ? "crit" : n >= 2 ? "warn" : n >= 1 ? "info" : "ok")
const num = (n: number | undefined, d = 1) => (typeof n === "number" && Number.isFinite(n) ? n.toFixed(d) : "-")
const rate = (bps?: number) => (typeof bps === "number" ? `${bytes(bps)}/s` : "-")

function Tile({
	label,
	value,
	sub,
	pct,
	status,
	spark,
}: {
	label: string
	value: string
	sub?: string
	pct?: number
	status?: string
	spark?: number[]
}) {
	return (
		<div className="rounded-lg border border-border bg-card p-3 flex flex-col gap-1.5">
			<div className="flex items-center justify-between gap-2">
				<span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
				{status ? <span className={`block size-2 rounded-full ${dotFor(status)}`} /> : null}
			</div>
			<div className="text-2xl font-semibold tabular-nums leading-none">{value}</div>
			{typeof pct === "number" ? (
				<div className="h-1.5 rounded-full bg-border overflow-hidden">
					<div className="h-full bg-primary transition-[width]" style={{ width: `${clamp(pct)}%` }} />
				</div>
			) : null}
			{sub ? <div className="text-xs text-muted-foreground truncate">{sub}</div> : null}
			{spark?.length ? <Sparkline values={spark} className="w-full" width={220} height={26} /> : null}
		</div>
	)
}

export default memo(() => {
	const d = useMaintenanceFile<Live>("live.json")
	const h = d?.host ?? {}
	const mem = h.mem ?? {}
	const memPct = mem.total ? clamp(((mem.used ?? 0) / mem.total) * 100) : undefined
	const cores = h.cores ?? 0
	const load1 = h.load?.[0]
	const loadPct = cores && typeof load1 === "number" ? clamp((load1 / cores) * 100) : undefined
	const gpu = d?.gpu ?? {}
	const gpuMemPct = gpu.mem_total ? clamp(((gpu.mem_used ?? 0) / gpu.mem_total) * 100) : undefined
	const busiest = [...(h.io ?? [])].sort((a, b) => (b.util_pct ?? 0) - (a.util_pct ?? 0))[0]
	const swap = d?.swap ?? {}
	const psi = h.psi ?? {}
	const lvl = d?.pressure?.level ?? 0
	const age = d?.generated_at ? Math.max(0, Math.round(Date.now() / 1000 - d.generated_at)) : undefined
	const fresh = age == null ? "none" : age > 60 ? "crit" : age > 20 ? "warn" : "ok"

	return (
		<>
			<div className="mb-4 flex flex-wrap items-end justify-between gap-3">
				<div>
					<h1 className="text-2xl font-semibold mb-1">
						<Trans>Live</Trans>
					</h1>
					<p className="text-sm text-muted-foreground">
						{d?.host?.uptime_s ? `up ${dur(h.uptime_s)} · ` : ""}
						{cores || "?"} <Trans>cores</Trans> · {d?.containers?.running ?? 0} <Trans>containers</Trans>
						{d?.interval_s ? ` · every ${Math.round(d.interval_s)}s` : ""}
					</p>
				</div>
				<span className="flex items-center gap-2 text-sm text-muted-foreground">
					<span className={`block size-2 rounded-full ${dotFor(fresh)}`} />
					{d?.generated_at ? `updated ${when(d.generated_at)}` : "waiting for the live monitor"}
				</span>
			</div>

			{!d ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>The live monitor has not published yet.</Trans>
				</div>
			) : (
				<>
					<div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
						<Tile
							label="CPU"
							value={`${num(h.cpu_pct)}%`}
							sub={`user ${num(h.cpu_user)} · sys ${num(h.cpu_sys)} · iowait ${num(h.cpu_iowait)}`}
							pct={h.cpu_pct}
							status={tone(h.cpu_pct, 85, 95)}
							spark={d.history?.cpu}
						/>
						<Tile
							label={t`Memory`}
							value={`${num(memPct, 0)}%`}
							sub={`${bytes(mem.used)} of ${bytes(mem.total)} · ${bytes(mem.avail)} free`}
							pct={memPct}
							status={tone(memPct, 85, 93)}
							spark={d.history?.mem_pct}
						/>
						<Tile
							label="Swap"
							value={`${num(swap.used_pct, 0)}%`}
							sub={`${swap.state ?? ""} · in ${rate(swap.in_bps)} · out ${rate(swap.out_bps)}`}
							pct={swap.used_pct}
							status={tone(swap.used_pct, 90, 98)}
							spark={d.history?.swap_pct}
						/>
						<Tile
							label="GPU"
							value={`${num(gpu.util, 0)}%`}
							sub={gpu.stale ? "reading is stale" : `${num(gpu.temp, 0)}°C · ${num(gpu.power_w, 0)} W`}
							pct={gpu.util}
							status={tone(gpu.util, 90, 97)}
							spark={d.history?.gpu}
						/>
						<Tile
							label="VRAM"
							value={`${num(gpuMemPct, 0)}%`}
							sub={`${bytes(gpu.mem_used)} of ${bytes(gpu.mem_total)}`}
							pct={gpuMemPct}
							status={tone(gpuMemPct, 90, 97)}
							spark={d.history?.vram_pct}
						/>
						<Tile
							label="Load"
							value={num(load1, 2)}
							sub={`${num(h.load?.[1], 2)} / ${num(h.load?.[2], 2)} · ${cores} cores`}
							pct={loadPct}
							status={tone(loadPct, 100, 150)}
							spark={d.history?.load1}
						/>
						<Tile
							label={t`Disk I/O`}
							value={`${num(busiest?.util_pct, 0)}%`}
							sub={
								busiest
									? `${busiest.dev} · r ${rate(busiest.read_bps)} · w ${rate(busiest.write_bps)}`
									: "No disk reading"
							}
							pct={busiest?.util_pct}
							spark={d.history?.disk_r}
						/>
						<Tile
							label={t`Network`}
							value={rate(h.net?.rx_bps)}
							sub={`up ${rate(h.net?.tx_bps)}`}
							spark={d.history?.net_rx}
						/>
					</div>

					<div className="grid gap-3 md:grid-cols-2 mb-6">
						<div className="rounded-lg border border-border bg-card p-4">
							<div className="flex items-center gap-2 mb-2">
								<span className={`block size-2.5 rounded-full ${dotFor(lvTone(lvl))}`} />
								<span className="text-sm font-semibold">
									<Trans>Pressure</Trans> · {lvl} {d.pressure?.level_name ? `(${d.pressure.level_name})` : ""}
								</span>
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
							<div className="text-sm font-semibold mb-2">
								<Trans>Temperatures & fans</Trans>
							</div>
							<div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
								{[
									["CPU", d.sensors?.cpu_temp],
									["GPU", d.sensors?.gpu_temp],
									["RAM", d.sensors?.ram_temp],
									["NVMe", d.sensors?.nvme_temp],
								].map(([k, v]) => (
									<div key={k as string} className="flex justify-between">
										<span className="text-muted-foreground">{k as string}</span>
										<span className="tabular-nums">{typeof v === "number" ? `${num(v, 0)}°C` : "-"}</span>
									</div>
								))}
								{[
									["CPU fan", d.sensors?.cpu_fan_rpm],
									["Case fans", d.sensors?.case_fan_rpm],
								].map(([k, v]) => (
									<div key={k as string} className="flex justify-between">
										<span className="text-muted-foreground">{k as string}</span>
										<span className="tabular-nums">{typeof v === "number" ? `${num(v, 0)} rpm` : "-"}</span>
									</div>
								))}
							</div>
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
										<span
											className={`block size-2 rounded-full ${dotFor(s.state === "up" ? "ok" : (s.state ?? ""))}`}
										/>
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
							<Table className="mb-6">
								<TableHeader>
									<TableRow>
										<TableHead>
											<Trans>Process</Trans>
										</TableHead>
										<TableHead className="w-48">
											<Trans>Container</Trans>
										</TableHead>
										<TableHead className="w-28 text-right">
											<Trans>Read</Trans>
										</TableHead>
										<TableHead className="w-28 text-right">
											<Trans>Write</Trans>
										</TableHead>
									</TableRow>
								</TableHeader>
								<TableBody>
									{(d.io_top?.readers ?? []).slice(0, 8).map((r, i) => (
										<TableRow key={i}>
											<TableCell className="font-mono text-xs">{r.name}</TableCell>
											<TableCell className="text-muted-foreground text-xs">{r.container ?? "—"}</TableCell>
											<TableCell className="text-right tabular-nums">{rate(r.read_bps)}</TableCell>
											<TableCell className="text-right tabular-nums">{rate(r.write_bps)}</TableCell>
										</TableRow>
									))}
								</TableBody>
							</Table>
						</>
					) : null}

					{(swap.holders?.length ?? 0) > 0 ? (
						<>
							<h2 className="text-lg font-semibold mb-2">
								<Trans>Swap held by</Trans>
							</h2>
							<Table className="mb-6">
								<TableHeader>
									<TableRow>
										<TableHead>
											<Trans>Who</Trans>
										</TableHead>
										<TableHead className="w-28">
											<Trans>Kind</Trans>
										</TableHead>
										<TableHead className="w-28 text-right">
											<Trans>Swap</Trans>
										</TableHead>
										<TableHead className="w-28 text-right">
											<Trans>Resident</Trans>
										</TableHead>
									</TableRow>
								</TableHeader>
								<TableBody>
									{(swap.holders ?? []).slice(0, 8).map((x, i) => (
										<TableRow key={i}>
											<TableCell className="truncate">{x.who}</TableCell>
											<TableCell className="text-muted-foreground">{x.kind ?? ""}</TableCell>
											<TableCell className="text-right tabular-nums">{bytes(x.swap_b)}</TableCell>
											<TableCell className="text-right tabular-nums text-muted-foreground">
												{bytes(x.resident_b)}
											</TableCell>
										</TableRow>
									))}
								</TableBody>
							</Table>
						</>
					) : null}

					{d.self ? (
						<p className="text-xs text-muted-foreground mb-2">
							<Trans>Live monitor</Trans> · tick {num(d.self.tick_ms)} ms · {num(d.self.rss_mb, 1)} MB ·{" "}
							{d.self.ticks ?? 0} <Trans>samples</Trans> · {d.self.errors ?? 0} <Trans>errors</Trans>
						</p>
					) : null}
				</>
			)}
			<FooterRepoLink />
		</>
	)
})
