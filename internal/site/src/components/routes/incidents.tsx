// Open incidents (Ohmz fork, see docs/OHMZ-REDESIGN.md).
// The maintenance engine keeps an incident ledger; its open entries are carried through each
// system's `info.mt.o`. Acknowledged ones are listed too (the owner knows), marked as such.
import { Trans, useLingui } from "@lingui/react/macro"
import { getPagePath } from "@nanostores/router"
import { useStore } from "@nanostores/react"
import { memo, useEffect, useMemo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { $router, Link } from "@/components/router"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { $systems } from "@/lib/stores"
import type { MaintenanceIncident } from "@/types"

const SEV_DOT: Record<string, string> = { sev1: "bg-red-500", sev2: "bg-red-500", sev3: "bg-yellow-500" }
const SEV_WORD: Record<string, string> = { sev1: "critical", sev2: "critical", sev3: "warning" }
const SEV_RANK: Record<string, number> = { sev1: 3, sev2: 2, sev3: 1 }

function since(ts?: number): string {
	if (!ts) return ""
	const diff = Math.round((Date.now() - ts * 1000) / 1000)
	const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
	if (diff < 3600) return rtf.format(-Math.max(1, Math.round(diff / 60)), "minute")
	if (diff < 86400) return rtf.format(-Math.round(diff / 3600), "hour")
	return rtf.format(-Math.round(diff / 86400), "day")
}

export default memo(() => {
	const { t } = useLingui()
	const systems = useStore($systems)

	useEffect(() => {
		document.title = `${t`Incidents`} / Beszel`
	}, [t])

	const rows = useMemo(() => {
		const out: { sysId: string; sys: string; inc: MaintenanceIncident }[] = []
		for (const s of systems) {
			for (const inc of s.info?.mt?.o ?? []) {
				out.push({ sysId: s.id, sys: s.name || s.info?.h || s.id, inc })
			}
		}
		out.sort((a, b) => (SEV_RANK[b.inc.s ?? ""] ?? 0) - (SEV_RANK[a.inc.s ?? ""] ?? 0) || (b.inc.t ?? 0) - (a.inc.t ?? 0))
		return out
	}, [systems])

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Incidents</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Open incidents from the maintenance engine's ledger, worst first.</Trans>
				</p>
			</div>
			{rows.length === 0 ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>No open incidents.</Trans>
				</div>
			) : (
				<Table>
					<TableHeader className="sticky top-0 z-10 bg-table-header">
						<TableRow>
							<TableHead className="w-28">
								<Trans>Severity</Trans>
							</TableHead>
							<TableHead>
								<Trans>Title</Trans>
							</TableHead>
							<TableHead className="w-40">
								<Trans>Task</Trans>
							</TableHead>
							<TableHead className="w-32">
								<Trans>Status</Trans>
							</TableHead>
							<TableHead className="w-28">
								<Trans>Open for</Trans>
							</TableHead>
							<TableHead className="w-40">
								<Trans>System</Trans>
							</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{rows.map(({ sysId, sys, inc }, i) => (
							<TableRow key={`${sysId}-${inc.i}-${i}`}>
								<TableCell>
									<span className="flex items-center gap-1.5">
										<span className={`block size-2 rounded-full ${SEV_DOT[inc.s ?? ""] ?? "bg-foreground/40"}`} />
										<span className="text-muted-foreground">{SEV_WORD[inc.s ?? ""] ?? inc.s ?? ""}</span>
									</span>
								</TableCell>
								<TableCell className="font-medium">
									{inc.n || inc.k || ""}
									{inc.d ? <span className="block text-xs text-muted-foreground truncate max-w-lg">{inc.d}</span> : null}
								</TableCell>
								<TableCell className="text-muted-foreground">{inc.k ?? ""}</TableCell>
								<TableCell>
									{inc.y === "acknowledged" ? (
										<span className="text-muted-foreground">
											<Trans>Acknowledged</Trans>
										</span>
									) : (
										<span className="text-red-500">
											<Trans>Open</Trans>
										</span>
									)}
								</TableCell>
								<TableCell className="tabular-nums text-muted-foreground whitespace-nowrap">{since(inc.t)}</TableCell>
								<TableCell>
									<Link href={getPagePath($router, "system", { id: sysId })} className="hover:underline">
										{sys}
									</Link>
								</TableCell>
							</TableRow>
						))}
					</TableBody>
				</Table>
			)}
			<FooterRepoLink />
		</>
	)
})
