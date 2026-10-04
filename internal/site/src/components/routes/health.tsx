import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { dotFor, dur, useMaintenanceFile, when } from "@/lib/maintenance"

type SelfCheck = { id?: string; title?: string; state?: string; detail?: string; age_s?: number; limit_s?: number }
type Self = {
	level?: string
	headline?: string
	verdict?: { level?: string; reasons?: string[]; since?: number }
	checks?: SelfCheck[]
	valid_until?: number
}

export default memo(() => {
	const s = useMaintenanceFile<Self>("self.json")
	const checks = s?.checks ?? []
	const reasons = s?.verdict?.reasons ?? []
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Pipeline health</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>The monitoring pipeline's own verdict — whether the checks are actually running.</Trans>
				</p>
			</div>

			<div className="rounded-lg border border-border bg-card p-4 mb-6 flex items-center gap-3">
				<span className={`block size-2.5 rounded-full ${dotFor(s?.level)}`} />
				<span className="text-sm font-medium">{s?.headline ?? "Waiting for the first data"}</span>
				{s?.valid_until ? (
					<span className="ms-auto text-xs text-muted-foreground">valid until {when(s.valid_until)}</span>
				) : null}
			</div>

			{reasons.length ? (
				<div className="rounded-lg border border-border bg-card p-4 mb-6">
					<h3 className="text-sm font-semibold mb-1">
						<Trans>Why</Trans>
					</h3>
					<ul className="text-sm text-muted-foreground list-disc ps-5">
						{reasons.map((r, i) => (
							<li key={i}>{r}</li>
						))}
					</ul>
				</div>
			) : null}

			<Table>
				<TableHeader className="sticky top-0 z-10 bg-table-header">
					<TableRow>
						<TableHead className="w-24">
							<Trans>State</Trans>
						</TableHead>
						<TableHead className="w-56">
							<Trans>Check</Trans>
						</TableHead>
						<TableHead>
							<Trans>Detail</Trans>
						</TableHead>
						<TableHead className="w-24 text-right">
							<Trans>Age</Trans>
						</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{checks.map((c, i) => (
						<TableRow key={c.id ?? i}>
							<TableCell>
								<span className="flex items-center gap-1.5">
									<span className={`block size-2 rounded-full ${dotFor(c.state)}`} />
									{c.state ?? ""}
								</span>
							</TableCell>
							<TableCell className="font-medium">{c.title || c.id || ""}</TableCell>
							<TableCell className="text-sm text-muted-foreground">{c.detail ?? ""}</TableCell>
							<TableCell className="text-right tabular-nums text-muted-foreground">
								{typeof c.age_s === "number" ? dur(c.age_s) : "-"}
							</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>
			<FooterRepoLink />
		</>
	)
})
