import { Trans } from "@lingui/react/macro"
import { memo, useMemo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { dotFor, useMaintenanceFile, when } from "@/lib/maintenance"

const RANK: Record<string, number> = { crit: 0, error: 0, warn: 1, info: 2, ok: 3, skipped: 4 }
type Check = { name: string; title?: string; klass?: string; status?: string; summary?: string; last_run?: number }

export default memo(() => {
	const d = useMaintenanceFile<{ checks?: Check[] }>("checks.json")
	const rows = useMemo(
		() => [...(d?.checks ?? [])].sort((a, b) => (RANK[a.status ?? ""] ?? 9) - (RANK[b.status ?? ""] ?? 9)),
		[d]
	)
	const bad = rows.filter((r) => r.status === "warn" || r.status === "crit" || r.status === "error").length
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Checks</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Every maintenance check, problems first.</Trans>
					{rows.length ? ` · ${rows.length - bad} ok, ${bad} need attention` : ""}
				</p>
			</div>
			{!rows.length ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>No check data yet.</Trans>
				</div>
			) : (
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
			)}
			<FooterRepoLink />
		</>
	)
})
