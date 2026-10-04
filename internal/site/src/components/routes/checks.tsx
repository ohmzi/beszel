import { Trans } from "@lingui/react/macro"
import { memo, useMemo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { dotFor, dur, useMaintenanceFile, when } from "@/lib/maintenance"

const RANK: Record<string, number> = { crit: 0, error: 0, warn: 1, info: 2, ok: 3, skipped: 4 }
type Check = { name: string; title?: string; klass?: string; status?: string; summary?: string; last_run?: number }
type Overview = {
	overall?: string
	headline?: string
	counts?: Record<string, number>
	paused?: boolean
	cleanup_mode?: string
	host?: string
	kernel?: string
	uptime_s?: number
	tiers?: Record<string, { last_run?: number; next_run?: number | null; mode?: string }>
}
type Day = { day?: string; worst?: string; warn_minutes?: number; crit_minutes?: number }

const TIERS: [string, string][] = [
	["check", "Checks"],
	["daily", "Daily cleanup"],
	["weekly", "Weekly review"],
]

export default memo(() => {
	const d = useMaintenanceFile<{ checks?: Check[] }>("checks.json")
	const ov = useMaintenanceFile<Overview>("overview.json")
	const hist = useMaintenanceFile<{ days?: Day[] }>("health-history.json")
	const acks = useMaintenanceFile<{
		acks?: {
			id?: string
			title?: string
			summary?: string
			severity?: string
			acked_at?: number
			until?: number
			days_left?: number
			by?: string
			note?: string
			active?: boolean
		}[]
	}>("acks.json")
	const rows = useMemo(
		() => [...(d?.checks ?? [])].sort((a, b) => (RANK[a.status ?? ""] ?? 9) - (RANK[b.status ?? ""] ?? 9)),
		[d]
	)
	const bad = rows.filter((r) => r.status === "warn" || r.status === "crit" || r.status === "error").length
	const counts = ov?.counts ?? {}
	const days = hist?.days ?? []

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1 flex items-center gap-2">
					{ov ? <span className={`block size-2.5 rounded-full ${dotFor(ov.overall)}`} /> : null}
					<Trans>Checks</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					{ov?.headline ?? <Trans>Every maintenance check, problems first.</Trans>}
					{ov?.host ? ` · ${ov.host}` : ""}
					{ov?.uptime_s ? ` · up ${dur(ov.uptime_s)}` : ""}
					{ov?.kernel ? ` · ${ov.kernel}` : ""}
				</p>
				{ov?.paused ? (
					<p className="text-sm text-yellow-500 mt-1">
						<Trans>Maintenance is paused (kill switch on): no cleanup actions run.</Trans>
					</p>
				) : null}
			</div>

			{ov ? (
				<>
					<div className="flex flex-wrap gap-2 mb-4 text-xs">
						{["ok", "warn", "crit", "error", "info"].map((k) =>
							counts[k] ? (
								<span
									key={k}
									className="inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5"
								>
									<span className={`block size-2 rounded-full ${dotFor(k)}`} />
									{counts[k]} {k}
								</span>
							) : null
						)}
						{ov.cleanup_mode ? (
							<span className="inline-flex items-center rounded-full border border-border px-2 py-0.5 text-muted-foreground">
								<Trans>Cleanup</Trans>: {ov.cleanup_mode}
							</span>
						) : null}
					</div>
					<div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6">
						{TIERS.map(([key, label]) => {
							const t = ov.tiers?.[key]
							return (
								<div key={key} className="rounded-lg border border-border bg-card p-3">
									<div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
									<div className="text-sm mt-1">
										{t?.last_run ? (
											<>
												<Trans>last</Trans> {when(t.last_run)}
											</>
										) : (
											<Trans>not run yet</Trans>
										)}
									</div>
									<div className="text-xs text-muted-foreground">
										{t?.next_run ? (
											<>
												<Trans>next</Trans> {when(t.next_run)}
											</>
										) : (
											<Trans>not scheduled</Trans>
										)}
										{t?.mode ? ` · ${t.mode}` : ""}
									</div>
								</div>
							)
						})}
					</div>
				</>
			) : null}

			{days.length ? (
				<>
					<h2 className="text-lg font-semibold mb-2">
						<Trans>30-day health calendar</Trans>
					</h2>
					<div className="flex flex-wrap gap-1.5 mb-6">
						{days.map((x) => (
							<span
								key={x.day}
								title={`${x.day} · ${x.worst ?? "unknown"} · ${x.warn_minutes ?? 0} min warning, ${x.crit_minutes ?? 0} min critical`}
								className={`block size-6 rounded ${dotFor(x.worst === "unknown" ? "skipped" : x.worst)}`}
							/>
						))}
					</div>
				</>
			) : null}

			<h2 className="text-lg font-semibold mb-2">
				<Trans>Right now</Trans>
			</h2>
			{!rows.length ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>No check data yet.</Trans>
				</div>
			) : (
				<>
					<p className="text-sm text-muted-foreground mb-3">
						{rows.length} <Trans>checks</Trans> · {bad} <Trans>need attention</Trans>
					</p>
					<Table>
						<TableHeader className="sticky top-0 z-10 bg-table-header">
							<TableRow>
								<TableHead className="w-28">
									<Trans>Status</Trans>
								</TableHead>
								<TableHead className="w-52">
									<Trans>Check</Trans>
								</TableHead>
								<TableHead>
									<Trans>Summary</Trans>
								</TableHead>
								<TableHead className="w-28">
									<Trans>Last run</Trans>
								</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{rows.map((c) => (
								<TableRow key={c.name}>
									<TableCell>
										<span className="flex items-center gap-1.5">
											<span className={`block size-2 rounded-full ${dotFor(c.status)}`} />
											<span className="text-muted-foreground">{c.status ?? ""}</span>
										</span>
									</TableCell>
									<TableCell>
										<span className="font-medium">{c.title || c.name}</span>
										<span className="block text-xs text-muted-foreground font-mono">
											{c.klass ? `${c.klass} · ` : ""}
											{c.name}
										</span>
									</TableCell>
									<TableCell className="text-sm">{c.summary ?? ""}</TableCell>
									<TableCell className="text-muted-foreground whitespace-nowrap">{when(c.last_run)}</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
				</>
			)}
			{(acks?.acks?.length ?? 0) > 0 ? (
				<>
					<h2 className="text-lg font-semibold mt-8 mb-2">
						<Trans>Acknowledged issues</Trans>
					</h2>
					<p className="text-sm text-muted-foreground mb-3">
						{acks?.acks?.length} · <Trans>no alerts for these until they expire</Trans>
					</p>
					<ul className="space-y-2">
						{(acks?.acks ?? []).map((a) => (
							<li key={a.id} className="rounded-lg border border-border bg-card p-3">
								<div className="flex items-center gap-2">
									<span className={`block size-2 rounded-full ${dotFor("acknowledged")}`} />
									<span className="font-medium text-sm">{a.title}</span>
									<span className="ms-auto text-xs text-muted-foreground whitespace-nowrap">
										{a.until ? `until ${new Date(a.until * 1000).toLocaleDateString()}` : ""}
										{typeof a.days_left === "number" ? ` (${a.days_left} days left)` : ""}
									</span>
								</div>
								{a.summary ? <p className="text-sm text-muted-foreground mt-1">{a.summary}</p> : null}
								<p className="text-xs text-muted-foreground mt-1">
									<Trans>acknowledged</Trans> {when(a.acked_at)}
									{a.by ? ` · ${a.by}` : ""}
									{a.note ? ` · ${a.note}` : ""}
									{a.active === false ? " · expired" : ""}
								</p>
							</li>
						))}
					</ul>
				</>
			) : null}

			<FooterRepoLink />
		</>
	)
})
