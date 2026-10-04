import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Link } from "@/components/router"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { bytes, dotFor, dur, useMaintenanceReport, when } from "@/lib/maintenance"

type Report = {
	id?: string
	kind?: string
	generated_at?: number
	headline?: string
	digest_text?: string
	health?: { score?: number; grade?: string; worst_status?: string; time_ok_pct?: number }
	period?: { label?: string; tz?: string; days?: number; hours?: number }
	highlights?: string[]
	notes?: string[]
	actions?: {
		count?: number
		freed_bytes?: number
		failed?: number
		refused?: number
		dry_run?: number
		would_free?: number
		alerts_sent?: number
		alerts_failed?: number
		by_task?: Record<string, { actions?: number; freed?: number }>
		notable?: { ts?: number; title?: string; detail?: string; source?: string }[]
	}
	incidents?: {
		opened?: number
		resolved?: number
		open_now?: number
		mttr_s?: number
		list?: { id?: string; title?: string; severity?: string; duration_s?: number; resolved?: boolean }[]
	}
	spikes?: {
		count?: number
		worst_level?: number
		oom_kills?: number
		handled_without_harm?: boolean
		list?: { t?: number; level?: number; outcome?: string }[]
	}
	capacity?: {
		mounts?: {
			mount?: string
			free_b?: number
			free_h?: string
			used_pct?: number
			days_to_full?: number | null
			trend_gib_per_day?: number
			note?: string
			level?: string
		}[]
		recommendations?: string[]
	}
	temperature?: {
		cpu_avg?: number
		cpu_max?: number
		gpu_avg?: number
		gpu_max?: number
		nvme_avg?: number
		nvme_max?: number
		hours_covered?: number
		hours_expected?: number
		fan_note?: string
	}
	slo?: { name?: string; availability_pct?: number; status?: string }[]
	upcoming?: { when?: string; what?: string }[]
}

const GRADE_BG: Record<string, string> = {
	A: "bg-green-500",
	B: "bg-yellow-500",
	C: "bg-orange-500",
	D: "bg-red-500",
	F: "bg-red-600",
}

const H2 = ({ children }: { children: React.ReactNode }) => (
	<h2 className="text-lg font-semibold mt-8 mb-2">{children}</h2>
)

export default memo(({ id }: { id: string }) => {
	const r = useMaintenanceReport<Report>(id)

	if (!r) {
		return (
			<>
				<Link href="/reports" className="text-sm text-muted-foreground hover:underline">
					‹ <Trans>All reports</Trans>
				</Link>
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm mt-4">
					<Trans>This report could not be loaded.</Trans>
				</div>
				<FooterRepoLink />
			</>
		)
	}

	const h = r.health ?? {}
	const a = r.actions ?? {}
	const inc = r.incidents ?? {}
	const sp = r.spikes ?? {}
	const temp = r.temperature ?? {}
	const byTask = Object.entries(a.by_task ?? {}).sort((x, y) => (y[1].freed ?? 0) - (x[1].freed ?? 0))

	return (
		<>
			<Link href="/reports" className="text-sm text-muted-foreground hover:underline">
				‹ <Trans>All reports</Trans>
			</Link>

			<div className="mt-3 mb-6 flex items-start gap-4">
				<span
					className={`inline-flex size-14 items-center justify-center rounded-xl text-2xl font-bold text-white ${GRADE_BG[h.grade ?? ""] ?? "bg-foreground/40"}`}
				>
					{h.grade ?? "?"}
				</span>
				<div>
					<h1 className="text-2xl font-semibold mb-1">
						{r.kind === "weekly" ? <Trans>Weekly report</Trans> : <Trans>Daily report</Trans>}
					</h1>
					<p className="text-sm text-muted-foreground">
						{r.period?.label ?? r.id} · <Trans>written</Trans> {when(r.generated_at)}
						{typeof h.score === "number" ? ` · ${h.score}/100` : ""}
					</p>
					<p className="text-base mt-1">{r.headline}</p>
				</div>
			</div>

			{r.digest_text ? (
				<p className="rounded-lg border border-border bg-card p-3 text-sm text-muted-foreground mb-4 whitespace-pre-wrap">
					{r.digest_text}
				</p>
			) : null}

			<div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
				<div className="rounded-lg border border-border bg-card p-3">
					<div className="text-xs uppercase tracking-wide text-muted-foreground">
						<Trans>Time healthy</Trans>
					</div>
					<div className="text-xl font-semibold tabular-nums">
						{typeof h.time_ok_pct === "number" ? `${h.time_ok_pct.toFixed(1)}%` : "-"}
					</div>
				</div>
				<div className="rounded-lg border border-border bg-card p-3">
					<div className="text-xs uppercase tracking-wide text-muted-foreground">
						<Trans>Incidents</Trans>
					</div>
					<div className="text-xl font-semibold tabular-nums">{inc.opened ?? 0}</div>
					<div className="text-xs text-muted-foreground">
						{inc.resolved ?? 0} <Trans>resolved</Trans>
						{inc.open_now ? ` · ${inc.open_now} open` : ""}
					</div>
				</div>
				<div className="rounded-lg border border-border bg-card p-3">
					<div className="text-xs uppercase tracking-wide text-muted-foreground">
						<Trans>Load spikes</Trans>
					</div>
					<div className="text-xl font-semibold tabular-nums">{sp.count ?? 0}</div>
					<div className="text-xs text-muted-foreground">
						{sp.oom_kills ? `${sp.oom_kills} OOM-killed` : sp.handled_without_harm ? "nothing harmed" : ""}
					</div>
				</div>
				<div className="rounded-lg border border-border bg-card p-3">
					<div className="text-xs uppercase tracking-wide text-muted-foreground">
						<Trans>Space freed</Trans>
					</div>
					<div className="text-xl font-semibold tabular-nums">{bytes(a.freed_bytes)}</div>
					<div className="text-xs text-muted-foreground">
						{a.count ?? 0} <Trans>actions</Trans>
					</div>
				</div>
			</div>

			{r.highlights?.length ? (
				<>
					<H2>
						<Trans>What mattered</Trans>
					</H2>
					<ul className="list-disc ps-5 text-sm space-y-1">
						{r.highlights.map((x, i) => (
							<li key={i}>{x}</li>
						))}
					</ul>
				</>
			) : null}

			{byTask.length || a.notable?.length ? (
				<>
					<H2>
						<Trans>Maintenance done</Trans>
					</H2>
					<p className="text-sm text-muted-foreground mb-2">
						{a.count ?? 0} <Trans>actions</Trans> · {a.failed ?? 0} <Trans>failed</Trans> · {a.refused ?? 0}{" "}
						<Trans>refused</Trans> · {a.dry_run ?? 0} <Trans>report-only</Trans> · {a.alerts_sent ?? 0}{" "}
						<Trans>alerts sent</Trans>
						{a.alerts_failed ? ` · ${a.alerts_failed} failed` : ""}
					</p>
					{byTask.length ? (
						<Table>
							<TableHeader>
								<TableRow>
									<TableHead>
										<Trans>Task</Trans>
									</TableHead>
									<TableHead className="w-24 text-right">
										<Trans>Actions</Trans>
									</TableHead>
									<TableHead className="w-28 text-right">
										<Trans>Freed</Trans>
									</TableHead>
								</TableRow>
							</TableHeader>
							<TableBody>
								{byTask.map(([name, v]) => (
									<TableRow key={name}>
										<TableCell className="font-mono text-xs">{name}</TableCell>
										<TableCell className="text-right tabular-nums">{v.actions ?? 0}</TableCell>
										<TableCell className="text-right tabular-nums">{bytes(v.freed)}</TableCell>
									</TableRow>
								))}
							</TableBody>
						</Table>
					) : null}
					{a.notable?.length ? (
						<ul className="mt-3 space-y-1 text-sm">
							{a.notable.map((x, i) => (
								<li key={i} className="py-1 border-t border-border/40">
									<span className="text-muted-foreground">{x.title}</span>
									{x.detail ? <span className="block text-xs text-muted-foreground">{x.detail}</span> : null}
								</li>
							))}
						</ul>
					) : null}
				</>
			) : null}

			{inc.list?.length ? (
				<>
					<H2>
						<Trans>Incidents</Trans>
					</H2>
					<ul className="text-sm">
						{inc.list.map((x, i) => (
							<li key={i} className="flex items-center gap-2 py-1 border-t border-border/40">
								<span className={`block size-2 rounded-full ${dotFor(x.severity === "sev3" ? "warn" : "crit")}`} />
								<span>{x.title}</span>
								<span className="ms-auto text-xs text-muted-foreground">
									{x.id} · {dur(x.duration_s)} · {x.resolved ? "resolved" : "still open"}
								</span>
							</li>
						))}
					</ul>
				</>
			) : null}

			{r.capacity?.mounts?.length ? (
				<>
					<H2>
						<Trans>Capacity outlook</Trans>
					</H2>
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>
									<Trans>Disk</Trans>
								</TableHead>
								<TableHead className="w-28 text-right">
									<Trans>Free</Trans>
								</TableHead>
								<TableHead className="w-24 text-right">
									<Trans>Used</Trans>
								</TableHead>
								<TableHead className="w-32 text-right">
									<Trans>Growth</Trans>
								</TableHead>
								<TableHead className="w-28 text-right">
									<Trans>Full in</Trans>
								</TableHead>
								<TableHead className="w-16">
									<Trans>Status</Trans>
								</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{(r.capacity.mounts ?? []).map((m) => (
								<TableRow key={m.mount}>
									<TableCell className="font-mono text-xs">
										{m.mount}
										{m.note ? <span className="block text-xs text-muted-foreground">{m.note}</span> : null}
									</TableCell>
									<TableCell className="text-right tabular-nums">{m.free_h || bytes(m.free_b)}</TableCell>
									<TableCell className="text-right tabular-nums">{Math.round(m.used_pct ?? 0)}%</TableCell>
									<TableCell className="text-right tabular-nums text-muted-foreground">
										{typeof m.trend_gib_per_day === "number" ? `${m.trend_gib_per_day.toFixed(1)} GiB/d` : "-"}
									</TableCell>
									<TableCell className="text-right tabular-nums">
										{typeof m.days_to_full === "number" ? `${Math.round(m.days_to_full)} d` : "-"}
									</TableCell>
									<TableCell>
										<span className={`block size-2 rounded-full ${dotFor(m.level)}`} />
									</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
					{r.capacity.recommendations?.length ? (
						<ul className="list-disc ps-5 text-sm mt-3 space-y-1">
							{r.capacity.recommendations.map((x, i) => (
								<li key={i}>{x}</li>
							))}
						</ul>
					) : null}
				</>
			) : null}

			{typeof temp.cpu_avg === "number" ? (
				<>
					<H2>
						<Trans>Temperatures</Trans>
					</H2>
					<div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
						{[
							["CPU", temp.cpu_avg, temp.cpu_max],
							["GPU", temp.gpu_avg, temp.gpu_max],
							["NVMe", temp.nvme_avg, temp.nvme_max],
						].map(([k, avg, max]) => (
							<div key={k as string} className="rounded-lg border border-border bg-card p-3">
								<div className="text-xs uppercase tracking-wide text-muted-foreground">{k as string}</div>
								<div className="text-xl font-semibold tabular-nums">
									{typeof avg === "number" ? `${Math.round(avg)}°C` : "-"}
								</div>
								<div className="text-xs text-muted-foreground">
									peak {typeof max === "number" ? `${Math.round(max)}°C` : "-"}
								</div>
							</div>
						))}
					</div>
				</>
			) : null}

			{r.slo?.length ? (
				<>
					<H2>
						<Trans>Service levels</Trans>
					</H2>
					<div className="grid gap-2 sm:grid-cols-2">
						{r.slo.map((o) => (
							<div key={o.name} className="flex items-center gap-2 text-sm py-1 border-t border-border/40">
								<span className={`block size-2 rounded-full ${dotFor(o.status)}`} />
								<span>{o.name}</span>
								<span className="ms-auto tabular-nums text-muted-foreground">
									{typeof o.availability_pct === "number" ? `${o.availability_pct.toFixed(2)}%` : ""}
								</span>
							</div>
						))}
					</div>
				</>
			) : null}

			{r.upcoming?.length ? (
				<>
					<H2>
						<Trans>Coming up</Trans>
					</H2>
					<ul className="text-sm">
						{r.upcoming.map((x, i) => (
							<li key={i} className="py-1 border-t border-border/40">
								<span className="text-muted-foreground">{x.when}</span> — {x.what}
							</li>
						))}
					</ul>
				</>
			) : null}

			{r.notes?.length ? (
				<>
					<H2>
						<Trans>About this report</Trans>
					</H2>
					<ul className="list-disc ps-5 text-sm text-muted-foreground space-y-1">
						{r.notes.map((x, i) => (
							<li key={i}>{x}</li>
						))}
					</ul>
				</>
			) : null}

			<FooterRepoLink />
		</>
	)
})
