import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { bytes, dotFor, useMaintenanceFile, when } from "@/lib/maintenance"

type Action = { ts?: number; task?: string; action?: string; target?: string; bytes?: number; outcome?: string }
type Step = { task?: string; title?: string; class?: string; mode?: string; state?: string }
type Cadence = { name?: string; cadence?: string; window?: string; counts?: Record<string, number>; steps?: Step[] }

const tile = (label: string, value: string, sub?: string) => (
	<div className="rounded-lg border border-border bg-card p-3">
		<div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
		<div className="text-xl font-semibold tabular-nums">{value}</div>
		{sub ? <div className="text-xs text-muted-foreground">{sub}</div> : null}
	</div>
)

export default memo(() => {
	const actions = useMaintenanceFile<{ recent?: Action[]; totals?: Record<string, number> }>("actions.json")
	const routine = useMaintenanceFile<{ routine?: Cadence[] }>("routine.json")
	const t = actions?.totals ?? {}
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Maintenance</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>What the engine did, and what the routine is doing.</Trans>
				</p>
			</div>

			<div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-6">
				{tile("Actions · 24h", String(t.actions_24h ?? 0), `${t.actions_7d ?? 0} this week`)}
				{tile("Freed · 24h", bytes(t.freed_24h), `${bytes(t.freed_7d)} this week`)}
				{tile("Freed · 30d", bytes(t.freed_30d))}
				{tile("Actions · 30d", String(t.actions_30d ?? 0))}
			</div>

			<h2 className="text-lg font-semibold mb-2">
				<Trans>Recent actions</Trans>
			</h2>
			{!actions?.recent?.length ? (
				<div className="rounded-lg border border-border bg-card px-4 py-8 text-center text-muted-foreground text-sm mb-6">
					<Trans>No actions recorded.</Trans>
				</div>
			) : (
				<Table>
					<TableHeader className="bg-table-header">
						<TableRow>
							<TableHead className="w-28">
								<Trans>When</Trans>
							</TableHead>
							<TableHead className="w-40">
								<Trans>Task</Trans>
							</TableHead>
							<TableHead>
								<Trans>Action</Trans>
							</TableHead>
							<TableHead className="w-32">
								<Trans>Outcome</Trans>
							</TableHead>
							<TableHead className="w-24 text-right">
								<Trans>Freed</Trans>
							</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{actions.recent.slice(0, 40).map((a, i) => (
							<TableRow key={i}>
								<TableCell className="text-muted-foreground whitespace-nowrap">{when(a.ts)}</TableCell>
								<TableCell className="font-mono text-xs">{a.task ?? ""}</TableCell>
								<TableCell>
									{a.action ?? ""}
									{a.target ? <span className="block text-xs text-muted-foreground font-mono">{a.target}</span> : null}
								</TableCell>
								<TableCell>
									<span className="flex items-center gap-1.5">
										<span className={`block size-2 rounded-full ${dotFor(a.outcome)}`} />
										{a.outcome ?? ""}
									</span>
								</TableCell>
								<TableCell className="text-right tabular-nums">{bytes(a.bytes)}</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
			)}

			<h2 className="text-lg font-semibold mt-8 mb-2">
				<Trans>Routine</Trans>
			</h2>
			<div className="grid gap-3 md:grid-cols-2">
				{(routine?.routine ?? []).map((c) => (
					<div key={c.name} className="rounded-lg border border-border bg-card p-4">
						<div className="flex items-baseline justify-between mb-2">
							<h3 className="text-sm font-semibold capitalize">{c.cadence || c.name || ""}</h3>
							<span className="text-xs text-muted-foreground">
								{c.window ?? ""}
								{c.counts
									? ` · ${Object.entries(c.counts)
											.map(([k, v]) => `${v} ${k}`)
											.join(", ")}`
									: ""}
							</span>
						</div>
						<ul className="text-sm">
							{(c.steps ?? []).map((s, i) => (
								<li key={i} className="flex items-center gap-2 py-0.5 border-t border-border/40">
									<span className={`block size-2 rounded-full ${dotFor(s.state || s.mode)}`} />
									<span>{s.title || s.task || ""}</span>
									<span className="ms-auto text-xs text-muted-foreground">{s.class ?? ""}</span>
								</li>
							))}
						</ul>
					</div>
				))}
			</div>
			<FooterRepoLink />
		</>
	)
})
