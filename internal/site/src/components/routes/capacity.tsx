import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Sparkline } from "@/components/sparkline"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { bytes, dotFor, useMaintenanceFile } from "@/lib/maintenance"

type Mount = {
	mount?: string
	used_pct?: number
	free_b?: number
	size_b?: number
	free_h?: string
	days?: number | null
	level?: string
	info?: boolean
}
type Freed = { day?: string; bytes?: number }
type Metrics = {
	current?: Record<string, number>
	avg_24h?: Record<string, number>
	avg_7d?: Record<string, number>
	series?: Record<string, number[]>
}

const rows: [string, string, string][] = [
	["ram_pct", "RAM in use", "%"],
	["cpu_pct", "CPU", "%"],
	["load1", "Load (1m)", ""],
	["cpu_temp", "CPU temp", "°C"],
	["gpu_temp", "GPU temp", "°C"],
	["nvme_temp", "NVMe temp", "°C"],
]

export default memo(() => {
	const s = useMaintenanceFile<{ mounts?: Mount[]; freed_by_day?: Freed[] }>("storage.json")
	const m = useMaintenanceFile<Metrics>("metrics.json")
	const mounts = (s?.mounts ?? []).filter((x) => !x.info)
	const soon = mounts
		.filter((x) => typeof x.days === "number" && x.days >= 0)
		.sort((a, b) => (a.days ?? 0) - (b.days ?? 0))
	const freed = s?.freed_by_day ?? []
	const freed30 = freed.reduce((n, x) => n + (x.bytes ?? 0), 0)
	const freedMax = Math.max(1, ...freed.map((x) => x.bytes ?? 0))
	const series = m?.series ?? {}
	const cur = m?.current ?? {}
	const avg = m?.avg_24h ?? {}
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Capacity</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Storage headroom and how soon each mount fills at the current rate.</Trans>
				</p>
			</div>

			<div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
				{soon.slice(0, 4).map((x) => (
					<div key={x.mount} className="rounded-lg border border-border bg-card p-3">
						<div className="text-xs text-muted-foreground truncate">{x.mount}</div>
						<div className="text-xl font-semibold tabular-nums">
							{typeof x.days === "number" ? `${Math.round(x.days)} d` : "-"}
						</div>
						<div className="text-xs text-muted-foreground">until full · {Math.round(x.used_pct ?? 0)}% used</div>
					</div>
				))}
				{freed.length ? (
					<div className="rounded-lg border border-border bg-card p-3">
						<div className="text-xs uppercase tracking-wide text-muted-foreground">
							<Trans>Freed · 30 days</Trans>
						</div>
						<div className="text-xl font-semibold tabular-nums">{bytes(freed30)}</div>
						<div className="text-xs text-muted-foreground">
							<Trans>by cleanup tasks</Trans>
						</div>
					</div>
				) : null}
			</div>

			<Table>
				<TableHeader className="sticky top-0 z-10 bg-table-header">
					<TableRow>
						<TableHead>
							<Trans>Mount</Trans>
						</TableHead>
						<TableHead className="w-24 text-right">
							<Trans>Used</Trans>
						</TableHead>
						<TableHead className="w-28 text-right">
							<Trans>Free</Trans>
						</TableHead>
						<TableHead className="w-28 text-right">
							<Trans>Size</Trans>
						</TableHead>
						<TableHead className="w-28 text-right">
							<Trans>Full in</Trans>
						</TableHead>
						<TableHead className="w-20">
							<Trans>Status</Trans>
						</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{mounts.map((x) => (
						<TableRow key={x.mount}>
							<TableCell className="font-mono text-xs">{x.mount}</TableCell>
							<TableCell className="text-right tabular-nums">{Math.round(x.used_pct ?? 0)}%</TableCell>
							<TableCell className="text-right tabular-nums">{x.free_h || bytes(x.free_b)}</TableCell>
							<TableCell className="text-right tabular-nums text-muted-foreground">{bytes(x.size_b)}</TableCell>
							<TableCell className="text-right tabular-nums">
								{typeof x.days === "number" && x.days >= 0
									? `${Math.round(x.days)} d`
									: x.days == null
										? "-"
										: "over a year"}
							</TableCell>
							<TableCell>
								<span className={`block size-2 rounded-full ${dotFor(x.level)}`} />
							</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>

			{freed.length ? (
				<>
					<h2 className="text-lg font-semibold mt-8 mb-2">
						<Trans>Space freed per day</Trans>
					</h2>
					<div className="flex items-end gap-[3px] h-16" aria-hidden="true">
						{freed.map((x) => (
							<div
								key={x.day}
								title={`${x.day} · ${bytes(x.bytes)}`}
								className="flex-1 rounded-t bg-primary/60 min-h-[2px]"
								style={{ height: `${Math.max(2, ((x.bytes ?? 0) / freedMax) * 100)}%` }}
							/>
						))}
					</div>
					<div className="flex justify-between text-xs text-muted-foreground mt-1">
						<span>{freed[0]?.day}</span>
						<span>
							<Trans>last 30 days</Trans>
						</span>
						<span>{freed[freed.length - 1]?.day}</span>
					</div>
				</>
			) : null}

			{series.ram_pct?.length ? (
				<>
					<h2 className="text-lg font-semibold mt-8 mb-2">
						<Trans>Memory, load & thermals — last 7 days</Trans>
					</h2>
					<div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
						{rows.map(([key, label, unit]) => (
							<div key={key} className="rounded-lg border border-border bg-card p-3">
								<div className="flex items-baseline justify-between">
									<span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
									<span className="text-lg font-semibold tabular-nums">
										{typeof cur[key] === "number" ? `${cur[key].toFixed(unit === "" ? 2 : 0)}${unit}` : "-"}
									</span>
								</div>
								<Sparkline values={series[key]} className="w-full mt-1" width={220} height={30} />
								<div className="text-xs text-muted-foreground">
									<Trans>24 h avg</Trans>{" "}
									{typeof avg[key] === "number" ? `${avg[key].toFixed(unit === "" ? 2 : 0)}${unit}` : "-"}
								</div>
							</div>
						))}
					</div>
				</>
			) : null}
			<FooterRepoLink />
		</>
	)
})
