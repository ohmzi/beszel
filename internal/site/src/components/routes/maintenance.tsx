import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { bytes, dotFor, useMaintenanceFile, when } from "@/lib/maintenance"

type Action = { ts?: number; task?: string; action?: string; target?: string; bytes?: number; outcome?: string }
type Step = { task?: string; title?: string; class?: string; mode?: string; state?: string }
type Cadence = { name?: string; cadence?: string; window?: string; counts?: Record<string, number>; steps?: Step[] }
type Journal = { ts?: number; title?: string; detail?: string }
type Job = {
	job?: string
	title?: string
	source?: string
	mode?: string
	class?: string
	heavy?: boolean
	schedule?: string
	next_due?: number
	last_start?: number | null
	last_status?: string | null
	last_summary?: string
	why?: string
}
type Timer = { unit?: string; title?: string; last?: number; next?: number | null; schedule?: string }
type Note = {
	ts?: number
	kind?: string
	severity?: string
	title?: string
	ok?: boolean
	channels?: string[]
	note?: string
	skipped?: string
}

const tile = (label: string, value: string, sub?: string) => (
	<div className="rounded-lg border border-border bg-card p-3">
		<div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
		<div className="text-xl font-semibold tabular-nums">{value}</div>
		{sub ? <div className="text-xs text-muted-foreground">{sub}</div> : null}
	</div>
)

const H2 = ({ children }: { children: React.ReactNode }) => (
	<h2 className="text-lg font-semibold mt-8 mb-2">{children}</h2>
)

export default memo(() => {
	const actions = useMaintenanceFile<{ recent?: Action[]; totals?: Record<string, number>; journal?: Journal[] }>(
		"actions.json"
	)
	const routine = useMaintenanceFile<{
		routine?: Cadence[]
		valid?: boolean
		errors?: string[]
		freeze?: { title?: string; from?: number; to?: number; active?: boolean }[]
		windows?: { name?: string; window?: string }[]
		state?: { halted?: string[] }
	}>("routine.json")
	const schedule = useMaintenanceFile<{ timers?: Timer[] }>("schedule.json")
	const jobs = useMaintenanceFile<{
		jobs?: Job[]
		counts?: Record<string, number>
		tick?: { status?: string; summary?: string }
		config_problems?: unknown
	}>("jobs.json")
	const notes = useMaintenanceFile<{
		counts?: { "24h"?: Record<string, number>; "7d"?: Record<string, number> }
		legs?: Record<
			string,
			{ ok?: number; fails?: number; last_ok?: number | null; last_fail?: number | null; broken?: boolean }
		>
		by_kind_24h?: Record<string, number>
		waiting?: number
		recent?: Note[]
	}>("notifications.json")

	const t = actions?.totals ?? {}
	const n24 = notes?.counts?.["24h"] ?? {}
	const n7 = notes?.counts?.["7d"] ?? {}

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Maintenance</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>What the engine did, what runs when, and what it told you.</Trans>
				</p>
			</div>

			<div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-2">
				{tile("Actions · 24h", String(t.actions_24h ?? 0), `${t.actions_7d ?? 0} this week`)}
				{tile("Freed · 24h", bytes(t.freed_24h), `${bytes(t.freed_7d)} this week`)}
				{tile("Freed · 30d", bytes(t.freed_30d))}
				{tile("Actions · 30d", String(t.actions_30d ?? 0))}
				{tile("Last change", actions?.journal?.[0]?.ts ? when(actions.journal[0].ts) : "Never")}
			</div>

			{routine &&
			(routine.valid === false || routine.errors?.length || routine.state?.halted?.length || routine.freeze?.length) ? (
				<div className="rounded-lg border border-border bg-card p-4 mt-4 text-sm space-y-1">
					{routine.valid === false ? (
						<p className="text-red-500">
							<Trans>The routine file is not valid — the scheduler is not running it.</Trans>
						</p>
					) : null}
					{(routine.errors ?? []).map((e, i) => (
						<p key={i} className="text-red-500">
							{e}
						</p>
					))}
					{(routine.state?.halted ?? []).map((x, i) => (
						<p key={i} className="text-yellow-500">
							<Trans>Halted:</Trans> {x}
						</p>
					))}
					{(routine.freeze ?? []).map((f, i) => (
						<p key={i} className="text-muted-foreground">
							<Trans>Change freeze</Trans>: {f.title ?? ""}{" "}
							{f.active ? (
								<span className="text-yellow-500">
									(<Trans>active now</Trans>)
								</span>
							) : null}
						</p>
					))}
				</div>
			) : null}

			<H2>
				<Trans>Recent actions</Trans>
			</H2>
			{!actions?.recent?.length ? (
				<div className="rounded-lg border border-border bg-card px-4 py-8 text-center text-muted-foreground text-sm">
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

			{(actions?.journal?.length ?? 0) > 0 ? (
				<>
					<H2>
						<Trans>Change log</Trans>
					</H2>
					<ul className="space-y-2">
						{(actions?.journal ?? []).slice(0, 12).map((j, i) => (
							<li key={i} className="rounded-lg border border-border bg-card p-3">
								<div className="flex items-baseline justify-between gap-3">
									<span className="text-sm font-medium">{j.title}</span>
									<span className="text-xs text-muted-foreground whitespace-nowrap">{when(j.ts)}</span>
								</div>
								{j.detail ? <p className="text-sm text-muted-foreground mt-1">{j.detail}</p> : null}
							</li>
						))}
					</ul>
				</>
			) : null}

			<H2>
				<Trans>Routine</Trans>
			</H2>
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

			{(schedule?.timers?.length ?? 0) > 0 ? (
				<>
					<H2>
						<Trans>Timers</Trans>
					</H2>
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>
									<Trans>Timer</Trans>
								</TableHead>
								<TableHead className="w-44">
									<Trans>Schedule</Trans>
								</TableHead>
								<TableHead className="w-28">
									<Trans>Last run</Trans>
								</TableHead>
								<TableHead className="w-28">
									<Trans>Next run</Trans>
								</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{(schedule?.timers ?? []).map((x) => (
								<TableRow key={x.unit}>
									<TableCell>
										<span className="font-medium">{x.title || x.unit}</span>
										<span className="block text-xs text-muted-foreground font-mono">{x.unit}</span>
									</TableCell>
									<TableCell className="text-muted-foreground font-mono text-xs">{x.schedule ?? ""}</TableCell>
									<TableCell className="text-muted-foreground">{when(x.last)}</TableCell>
									<TableCell className="text-muted-foreground">{x.next ? when(x.next) : "—"}</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
				</>
			) : null}

			{(jobs?.jobs?.length ?? 0) > 0 ? (
				<>
					<H2>
						<Trans>Everything that runs</Trans>
					</H2>
					{jobs?.tick?.summary ? (
						<p className="text-xs text-muted-foreground mb-2">
							<span className={`inline-block size-2 rounded-full me-1.5 ${dotFor(jobs.tick.status)}`} />
							<Trans>Scheduler:</Trans> {jobs.tick.summary}
						</p>
					) : null}
					<Table>
						<TableHeader>
							<TableRow>
								<TableHead>
									<Trans>What</Trans>
								</TableHead>
								<TableHead className="w-40">
									<Trans>Cadence</Trans>
								</TableHead>
								<TableHead className="w-32">
									<Trans>Last run</Trans>
								</TableHead>
								<TableHead className="w-28">
									<Trans>Next run</Trans>
								</TableHead>
								<TableHead className="w-16">
									<Trans>Class</Trans>
								</TableHead>
								<TableHead className="w-28">
									<Trans>Run by</Trans>
								</TableHead>
							</TableRow>
						</TableHeader>
						<TableBody>
							{(jobs?.jobs ?? []).slice(0, 60).map((j) => (
								<TableRow key={j.job}>
									<TableCell>
										<span className="font-medium">{j.title || j.job}</span>
										{j.why ? <span className="block text-xs text-muted-foreground">{j.why}</span> : null}
									</TableCell>
									<TableCell className="font-mono text-xs text-muted-foreground">{j.schedule ?? ""}</TableCell>
									<TableCell>
										<span className="flex items-center gap-1.5">
											{typeof j.last_start === "number" ? (
												<span className={`block size-2 rounded-full ${dotFor(j.last_status ?? "")}`} />
											) : null}
											<span className="text-muted-foreground">{j.last_start ? when(j.last_start) : "never"}</span>
										</span>
									</TableCell>
									<TableCell className="text-muted-foreground">{j.next_due ? when(j.next_due) : "—"}</TableCell>
									<TableCell className="text-muted-foreground">{j.class ?? ""}</TableCell>
									<TableCell className="text-muted-foreground">{j.source ?? ""}</TableCell>
								</TableRow>
							))}
						</TableBody>
					</Table>
				</>
			) : null}

			{notes ? (
				<>
					<H2>
						<Trans>Notifications</Trans>
					</H2>
					<div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
						{tile("Sent · 24h", String(n24.sent ?? 0), `${n24.sms ?? 0} SMS · ${n24.email ?? 0} email`)}
						{tile("Failed · 24h", String(n24.failed ?? 0), `${n24.skipped ?? 0} skipped`)}
						{tile("Sent · 7 days", String(n7.sent ?? 0))}
						{tile("Waiting to retry", String(notes.waiting ?? 0))}
					</div>
					<div className="space-y-1">
						{Object.entries(notes.legs ?? {}).map(([k, v]) => (
							<div key={k} className="flex items-center gap-2 text-sm">
								<span className={`block size-2 rounded-full ${dotFor(v.broken ? "crit" : "ok")}`} />
								<span className="capitalize">{k}</span>
								<span className="text-muted-foreground">
									{v.ok ?? 0} sent · {v.fails ?? 0} failed
									{v.last_ok ? ` · last ${when(v.last_ok)}` : ""}
								</span>
							</div>
						))}
					</div>
					{notes.recent?.length ? (
						<ul className="mt-3 space-y-1">
							{notes.recent.slice(0, 10).map((r, i) => (
								<li key={i} className="flex items-center gap-2 py-1 border-t border-border/40 text-sm">
									<span
										className={`block size-2 rounded-full ${dotFor(r.ok === false ? "failed" : r.skipped ? "skipped" : "done")}`}
									/>
									<span className="text-muted-foreground whitespace-nowrap">{when(r.ts)}</span>
									<span className="truncate">{r.title}</span>
									<span className="ms-auto text-xs text-muted-foreground whitespace-nowrap">
										{r.skipped || r.channels?.join(", ") || ""}
									</span>
								</li>
							))}
						</ul>
					) : null}
				</>
			) : null}

			<FooterRepoLink />
		</>
	)
})
