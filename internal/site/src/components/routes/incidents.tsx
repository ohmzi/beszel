// Open incidents (Ohmz fork, see docs/OHMZ-REDESIGN.md).
// The maintenance engine keeps an incident ledger; its open entries are carried through each
// system's `info.mt.o`. Acknowledged ones are listed too (the owner knows), marked as such.
// Acknowledge / Un-acknowledge here forwards a request to the agent, which writes a signed request
// into the engine's inbox; the runner applies it within a minute.
import { Trans, useLingui } from "@lingui/react/macro"
import { getPagePath } from "@nanostores/router"
import { useStore } from "@nanostores/react"
import { memo, useEffect, useMemo, useState } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { $router, Link } from "@/components/router"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { pb } from "@/lib/api"
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
	const [busy, setBusy] = useState<Record<string, string>>({})
	const [note, setNote] = useState<{ fp: string; text: string; ok: boolean } | null>(null)

	useEffect(() => {
		document.title = `${t`Incidents`} / OhmzMaintainer`
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

	async function send(sysId: string, kind: "ack" | "unack", fp: string) {
		setBusy((b) => ({ ...b, [fp]: kind }))
		setNote(null)
		try {
			await pb.send("/api/beszel/maintenance/ack", {
				method: "POST",
				query: { system: sysId },
				body: kind === "ack" ? { kind, fp, severity: "warn", days: 90 } : { kind, fp },
			})
			setNote({ fp, text: t`Requested. The runner applies it within a minute.`, ok: true })
		} catch (err) {
			setNote({ fp, text: String((err as Error)?.message || err), ok: false })
		} finally {
			setBusy((b) => {
				const n = { ...b }
				delete n[fp]
				return n
			})
		}
	}

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
							<TableHead className="w-36">
								<Trans>Task</Trans>
							</TableHead>
							<TableHead className="w-32">
								<Trans>Status</Trans>
							</TableHead>
							<TableHead className="w-24">
								<Trans>Open for</Trans>
							</TableHead>
							<TableHead className="w-32">
								<Trans>System</Trans>
							</TableHead>
							<TableHead className="w-40 text-right">
								<Trans>Acknowledge</Trans>
							</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{rows.map(({ sysId, sys, inc }, i) => {
							const fp = inc.p ?? ""
							const b = fp ? busy[fp] : undefined
							const canAck = !!fp && inc.s === "sev3" && inc.y !== "acknowledged"
							const canUnack = !!fp && inc.y === "acknowledged"
							return (
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
										{note && note.fp === fp ? (
											<span className={`block text-xs ${note.ok ? "text-green-500" : "text-red-500"}`}>{note.text}</span>
										) : null}
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
									<TableCell className="text-right">
										{canAck ? (
											<button
												type="button"
												className="inline-flex h-8 items-center rounded-md border border-border bg-card px-3 text-xs font-medium hover:bg-accent disabled:opacity-60"
												disabled={!!b}
												onClick={() => send(sysId, "ack", fp)}
											>
												{b === "ack" ? t`Sending…` : t`Acknowledge`}
											</button>
										) : canUnack ? (
											<button
												type="button"
												className="inline-flex h-8 items-center rounded-md border border-border bg-card px-3 text-xs font-medium hover:bg-accent disabled:opacity-60"
												disabled={!!b}
												onClick={() => send(sysId, "unack", fp)}
											>
												{b === "unack" ? t`Sending…` : t`Un-acknowledge`}
											</button>
										) : (
											<span className="text-xs text-muted-foreground">{inc.s === "sev3" ? "" : t`crit — not acknowledgeable`}</span>
										)}
									</TableCell>
								</TableRow>
							)
						})}
					</TableBody>
				</Table>
			)}
			<FooterRepoLink />
		</>
	)
})
