// Maintenance reports (Ohmz fork, see docs/OHMZ-REDESIGN.md).
// The engine publishes daily and weekly reports with a health score; its index is carried through
// each system's `info.mt.p`.
import { Trans, useLingui } from "@lingui/react/macro"
import { useStore } from "@nanostores/react"
import { memo, useEffect, useMemo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { $systems } from "@/lib/stores"
import type { MaintenanceReport } from "@/types"

const GRADE_BG: Record<string, string> = {
	A: "bg-green-500",
	B: "bg-yellow-500",
	C: "bg-orange-500",
	D: "bg-red-500",
	F: "bg-red-600",
}

function when(ts?: number): string {
	if (!ts) return ""
	return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

export default memo(() => {
	const { t } = useLingui()
	const systems = useStore($systems)

	useEffect(() => {
		document.title = `${t`Reports`} / OhmzMaintainer`
	}, [t])

	const rows = useMemo(() => {
		const out: { sysId: string; sys: string; r: MaintenanceReport }[] = []
		for (const s of systems) {
			for (const r of s.info?.mt?.p ?? []) out.push({ sysId: s.id, sys: s.name || s.info?.h || s.id, r })
		}
		out.sort((a, b) => (b.r.t ?? 0) - (a.r.t ?? 0))
		return out
	}, [systems])

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Reports</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Daily and weekly maintenance reports, newest first.</Trans>
				</p>
			</div>
			{rows.length === 0 ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>No reports yet.</Trans>
				</div>
			) : (
				<Table>
					<TableHeader className="sticky top-0 z-10 bg-table-header">
						<TableRow>
							<TableHead className="w-24">
								<Trans>Health</Trans>
							</TableHead>
							<TableHead className="w-28">
								<Trans>Kind</Trans>
							</TableHead>
							<TableHead className="w-32">
								<Trans>Period</Trans>
							</TableHead>
							<TableHead>
								<Trans>Headline</Trans>
							</TableHead>
							<TableHead className="w-44">
								<Trans>Generated</Trans>
							</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{rows.map(({ sysId, r }, i) => (
							<TableRow key={`${sysId}-${r.i}-${i}`}>
								<TableCell>
									<span className="flex items-center gap-2">
										<span
											className={`inline-flex size-6 items-center justify-center rounded-md text-xs font-semibold text-white ${GRADE_BG[r.g ?? ""] ?? "bg-foreground/40"}`}
										>
											{r.g ?? "?"}
										</span>
										<span className="tabular-nums text-muted-foreground">{r.s ?? ""}</span>
									</span>
								</TableCell>
								<TableCell className="text-muted-foreground">{r.k ?? ""}</TableCell>
								<TableCell className="font-medium">{r.i ?? ""}</TableCell>
								<TableCell>{r.n ?? ""}</TableCell>
								<TableCell className="text-muted-foreground whitespace-nowrap">{when(r.t)}</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
			)}
			<FooterRepoLink />
		</>
	)
})
