import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { dotFor, useMaintenanceFile } from "@/lib/maintenance"

type Obj = {
	name?: string
	class?: string
	target_pct?: number
	availability_pct?: number
	budget_remaining_pct?: number
	burn_rate_1d?: number
	status?: string
	bad_minutes?: number
	budget_minutes?: number
	observed_h?: number
	note?: string
}

const pct = (v?: number, d = 2) => (typeof v === "number" && Number.isFinite(v) ? `${v.toFixed(d)}%` : "-")

export default memo(() => {
	const d = useMaintenanceFile<{ window_days?: number; objectives?: Obj[] }>("slo.json")
	const objs = [...(d?.objectives ?? [])].sort((a, b) => (a.status === "ok" ? 1 : 0) - (b.status === "ok" ? 1 : 0))
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Service levels</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Availability objectives and error budget over the last {d?.window_days ?? 30} days.</Trans>
				</p>
			</div>
			{!objs.length ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>Service objectives are still collecting data.</Trans>
				</div>
			) : (
				<div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
					{objs.map((o) => {
						const spent = o.budget_remaining_pct == null ? 0 : Math.max(0, Math.min(100, 100 - o.budget_remaining_pct))
						return (
							<div key={o.name} className="rounded-lg border border-border bg-card p-4">
								<div className="flex items-center gap-2 mb-1">
									<span className={`block size-2 rounded-full ${dotFor(o.status)}`} />
									<h3 className="text-sm font-semibold truncate">{o.name}</h3>
									<span className="ms-auto text-xs text-muted-foreground">{o.class ?? ""}</span>
								</div>
								<div className="flex items-baseline gap-2">
									<span className="text-2xl font-semibold tabular-nums">{pct(o.availability_pct, 2)}</span>
									<span className="text-xs text-muted-foreground">
										target {pct(o.target_pct, o.target_pct && o.target_pct % 1 ? 1 : 0)}
									</span>
								</div>
								<div className="mt-2 h-1.5 rounded-full bg-border overflow-hidden">
									<div className="h-full bg-primary" style={{ width: `${spent}%` }} />
								</div>
								<p className="text-xs text-muted-foreground mt-1">
									{spent <= 0 ? "error budget untouched" : `${Math.round(spent)}% of the error budget used`}
									{typeof o.bad_minutes === "number" ? ` · ${o.bad_minutes} min down` : ""}
									{typeof o.burn_rate_1d === "number" && o.burn_rate_1d > 0.05
										? ` · burning ${o.burn_rate_1d.toFixed(1)}x`
										: ""}
								</p>
								{o.note ? <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{o.note}</p> : null}
							</div>
						)
					})}
				</div>
			)}
			<FooterRepoLink />
		</>
	)
})
