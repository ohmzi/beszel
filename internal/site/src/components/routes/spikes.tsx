import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { dotFor, dur, useMaintenanceFile, when } from "@/lib/maintenance"

type Spike = {
	t?: number
	state?: string
	peak_level?: number
	duration_s?: number
	outcome?: string
	nothing_killed?: boolean
	restarted?: string[]
	stopped?: string[]
	dims?: Record<string, number>
}
const DIMW: Record<string, string> = { mem: "memory", io: "disk I/O", cpu: "CPU", gpu: "GPU" }
const lvState = (n = 0) => (n >= 4 ? "crit" : n >= 2 ? "warn" : n >= 1 ? "info" : "ok")

export default memo(() => {
	const p = useMaintenanceFile<{ level?: number; level_name?: string; since?: number; spikes?: Spike[] }>(
		"pressure.json"
	)
	const spikes = [...(p?.spikes ?? [])].filter((s) => s.t).sort((a, b) => (b.t ?? 0) - (a.t ?? 0))
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Load spikes</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>
						Pressure episodes, the worst first. A spike is logged when memory, disk I/O or CPU pressure stays high for
						two checks in a row.
					</Trans>
				</p>
			</div>
			<div className="rounded-lg border border-border bg-card p-4 mb-6 flex items-center gap-3">
				<span className={`block size-2.5 rounded-full ${dotFor(lvState(p?.level))}`} />
				<span className="text-sm">
					{p?.level ? (
						<>
							<Trans>Level</Trans> {p.level}
							{p.level_name ? ` (${p.level_name})` : ""}
							{p.since ? ` since ${when(p.since)}` : ""}
						</>
					) : (
						<Trans>Calm right now</Trans>
					)}
				</span>
			</div>
			{!spikes.length ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>No load spikes recorded.</Trans>
				</div>
			) : (
				<Table>
					<TableHeader className="sticky top-0 z-10 bg-table-header">
						<TableRow>
							<TableHead className="w-28">
								<Trans>When</Trans>
							</TableHead>
							<TableHead className="w-20">
								<Trans>Level</Trans>
							</TableHead>
							<TableHead className="w-24">
								<Trans>Lasted</Trans>
							</TableHead>
							<TableHead>
								<Trans>Drivers</Trans>
							</TableHead>
							<TableHead className="w-64">
								<Trans>Outcome</Trans>
							</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{spikes.slice(0, 60).map((s, i) => {
							const dims = Object.keys(DIMW)
								.filter((k) => (s.dims?.[k] ?? 0) >= 1)
								.map((k) => DIMW[k])
							const acts = [
								...(s.restarted ?? []).map((n) => `restarted ${n}`),
								...(s.stopped ?? []).map((n) => `stopped ${n}`),
							]
							return (
								<TableRow key={i}>
									<TableCell className="text-muted-foreground whitespace-nowrap">{when(s.t)}</TableCell>
									<TableCell>
										<span className="flex items-center gap-1.5">
											<span className={`block size-2 rounded-full ${dotFor(lvState(s.peak_level))}`} />
											{Math.max(0, Math.min(5, Math.round(s.peak_level ?? 0)))}
										</span>
									</TableCell>
									<TableCell className="tabular-nums text-muted-foreground">{dur(s.duration_s)}</TableCell>
									<TableCell className="text-sm">{dims.join(", ") || "-"}</TableCell>
									<TableCell className="text-sm text-muted-foreground">
										{s.nothing_killed && !acts.length ? "nothing was killed" : acts.join(", ") || s.outcome || "-"}
									</TableCell>
								</TableRow>
							)
						})}
					</TableBody>
				</Table>
			)}
			<FooterRepoLink />
		</>
	)
})
