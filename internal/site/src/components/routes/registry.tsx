import { Trans } from "@lingui/react/macro"
import { memo, useMemo, useState } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { dotFor, useMaintenanceFile, when } from "@/lib/maintenance"
import { cn } from "@/lib/utils"

type Rule = {
	id?: string
	category?: string
	title?: string
	kind?: string
	why?: string
	does?: string
	applies_to?: string[]
	enabled?: boolean
	severity?: string
	destructive?: boolean
	mode?: string | null
	proof?: string
	principle?: string
	since?: string
	source_file?: string
	target?: string
	last_evaluated?: number | null
	last_triggered?: number | null
	triggers_30d?: number
}
type Category = { id?: string; title?: string; blurb?: string; count?: number }
type Registry = {
	generated_at?: number
	registry_hash?: string
	registry_synced_at?: number
	valid?: boolean
	applied?: boolean
	stats?: {
		total?: number
		enabled?: number
		disabled?: number
		destructive?: number
		apply_mode?: number
		evaluated_24h?: number
		triggered_30d?: number
		by_kind?: Record<string, number>
	}
	categories?: Category[]
	rules?: Rule[]
}

const KIND_LABEL: Record<string, string> = {
	check: "Check",
	cleanup: "Cleanup",
	protection: "Protection",
	alert: "Alert",
	schedule: "Schedule",
	probe: "Probe",
	job: "Job",
	spike: "Spike response",
	policy: "Policy",
	safety: "Safety limit",
}

const chip = "inline-flex items-center rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground"

export default memo(() => {
	const d = useMaintenanceFile<Registry>("rules.json")
	const [q, setQ] = useState("")
	const [cat, setCat] = useState("")
	const [open, setOpen] = useState<string | null>(null)
	const [limit, setLimit] = useState(40)

	const rules = d?.rules ?? []
	const filtered = useMemo(() => {
		const needle = q.trim().toLowerCase()
		return rules.filter(
			(r) =>
				(!cat || r.category === cat) &&
				(!needle ||
					`${r.id ?? ""} ${r.title ?? ""} ${r.why ?? ""} ${r.does ?? ""} ${r.category ?? ""} ${r.kind ?? ""}`
						.toLowerCase()
						.includes(needle))
		)
	}, [rules, q, cat])

	if (!d) {
		return (
			<>
				<div className="mb-4">
					<h1 className="text-2xl font-semibold mb-1">
						<Trans>Registry</Trans>
					</h1>
					<p className="text-sm text-muted-foreground">
						<Trans>Every rule the script runs under, read-only.</Trans>
					</p>
				</div>
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>The rules have not been published yet.</Trans>
				</div>
				<FooterRepoLink />
			</>
		)
	}

	const stats = d.stats ?? {}
	const cats = d.categories ?? []

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Registry</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Every rule the script runs under, read-only.</Trans>
				</p>
			</div>

			<div className="rounded-lg border border-border bg-card p-4 mb-6">
				<div className="flex flex-wrap items-center gap-2 mb-3">
					<span className="flex items-center gap-1.5 text-sm font-medium">
						<span className={`block size-2 rounded-full ${dotFor(d.valid ? "ok" : "crit")}`} />
						{d.valid ? <Trans>Valid</Trans> : <Trans>Does not validate</Trans>}
					</span>
					{d.registry_hash ? (
						<span className="font-mono text-xs text-muted-foreground">#{d.registry_hash.slice(0, 12)}</span>
					) : null}
					{d.registry_synced_at ? (
						<span className="text-xs text-muted-foreground">
							<Trans>synced</Trans> {when(d.registry_synced_at)}
						</span>
					) : null}
				</div>
				<div className="flex flex-wrap gap-2">
					<span className={chip}>
						{stats.total ?? rules.length} <Trans>rules</Trans>
					</span>
					<span className={chip}>
						{stats.enabled ?? "-"} <Trans>enabled</Trans>
					</span>
					<span className={chip}>
						{stats.destructive ?? 0} <Trans>destructive</Trans>
					</span>
					<span className={chip}>
						{stats.evaluated_24h ?? 0} <Trans>evaluated in 24 h</Trans>
					</span>
					<span className={chip}>
						{stats.triggered_30d ?? 0} <Trans>triggered in 30 days</Trans>
					</span>
				</div>
			</div>

			<div className="flex flex-wrap items-center gap-2 mb-4">
				<input
					value={q}
					onChange={(e) => {
						setQ(e.target.value)
						setLimit(40)
					}}
					placeholder="Search: disk, docker, 90 days ..."
					className="w-full sm:w-72 h-9 rounded-md border border-border bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring"
				/>
				<button
					type="button"
					onClick={() => {
						setCat("")
						setLimit(40)
					}}
					className={cn(chip, "h-9 px-3", !cat && "border-primary text-foreground")}
				>
					<Trans>All rules</Trans>
				</button>
				{cats.map((c) => (
					<button
						key={c.id}
						type="button"
						onClick={() => {
							setCat(c.id ?? "")
							setLimit(40)
						}}
						className={cn(chip, "h-9 px-3", cat === c.id && "border-primary text-foreground")}
					>
						{c.title ?? c.id} <span className="ms-1 text-muted-foreground/70">{c.count ?? ""}</span>
					</button>
				))}
			</div>

			<p className="text-sm text-muted-foreground mb-3">
				{cat || q ? (
					<>
						{filtered.length} <Trans>of</Trans> {rules.length} <Trans>rules match</Trans>
					</>
				) : (
					<>
						{rules.length} <Trans>rules</Trans>
					</>
				)}
			</p>

			{!filtered.length ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>No rule matches.</Trans>
				</div>
			) : (
				<div className="space-y-2">
					{filtered.slice(0, limit).map((r) => {
						const isOpen = open === r.id
						return (
							<div key={r.id} className="rounded-lg border border-border bg-card">
								<button
									type="button"
									onClick={() => setOpen(isOpen ? null : (r.id ?? null))}
									className="w-full text-left p-3 flex flex-col gap-2"
								>
									<div className="flex flex-wrap items-center gap-2">
										<span className="font-medium text-sm">{r.title || r.id}</span>
										{r.kind ? <span className={chip}>{KIND_LABEL[r.kind] ?? r.kind}</span> : null}
										{r.mode ? <span className={chip}>{r.mode}</span> : null}
										{r.destructive ? (
											<span className="inline-flex items-center rounded-full border border-red-500/40 px-2 py-0.5 text-[11px] text-red-500">
												<Trans>Destructive</Trans>
											</span>
										) : null}
										{r.enabled === false ? (
											<span className={chip}>
												<Trans>Disabled</Trans>
											</span>
										) : null}
										<span className="ms-auto font-mono text-[11px] text-muted-foreground">{r.id}</span>
									</div>
									{r.why ? <p className="text-sm text-muted-foreground line-clamp-2">{r.why}</p> : null}
								</button>
								{isOpen ? (
									<div className="border-t border-border p-3 text-sm space-y-3">
										{r.does ? (
											<div>
												<div className="text-xs uppercase tracking-wide text-muted-foreground mb-1">
													<Trans>What the script does</Trans>
												</div>
												<p className="whitespace-pre-wrap">{r.does}</p>
											</div>
										) : null}
										{r.proof ? (
											<div>
												<div className="text-xs uppercase tracking-wide text-muted-foreground mb-1">
													<Trans>Safety proof</Trans>
												</div>
												<p>{r.proof}</p>
											</div>
										) : null}
										<div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs text-muted-foreground">
											{r.source_file ? (
												<div>
													<span className="uppercase tracking-wide">Source</span>
													<div className="font-mono text-foreground">{r.source_file}</div>
												</div>
											) : null}
											{r.since ? (
												<div>
													<span className="uppercase tracking-wide">Since</span>
													<div className="text-foreground">{r.since}</div>
												</div>
											) : null}
											{r.principle ? (
												<div>
													<span className="uppercase tracking-wide">Principle</span>
													<div className="text-foreground">{r.principle}</div>
												</div>
											) : null}
											<div>
												<span className="uppercase tracking-wide">Activity</span>
												<div className="text-foreground">
													{r.last_evaluated ? `evaluated ${when(r.last_evaluated)} · ` : ""}
													{r.triggers_30d ? `${r.triggers_30d} triggers in 30 days` : "never triggered"}
												</div>
											</div>
										</div>
										{r.applies_to?.length ? (
											<div className="flex flex-wrap gap-1">
												{r.applies_to.map((a) => (
													<span key={a} className={chip}>
														{a}
													</span>
												))}
											</div>
										) : null}
									</div>
								) : null}
							</div>
						)
					})}
				</div>
			)}

			{filtered.length > limit ? (
				<button
					type="button"
					onClick={() => setLimit((n) => n + 40)}
					className="mt-4 h-9 rounded-md border border-border px-4 text-sm hover:bg-accent"
				>
					<Trans>Show {Math.min(40, filtered.length - limit)} more</Trans>
				</button>
			) : null}

			<FooterRepoLink />
		</>
	)
})
