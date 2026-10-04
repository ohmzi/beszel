// Central alerts feed (Ohmz fork, see docs/OHMZ-REDESIGN.md).
//
// Every issue the homelab-maint engine alerted about — its own alerts and the external ones bridged
// through it (Hermes, Kuma, ...) — is published to its delivery log and carried here through each
// system's `info.mt.r`. This is the one place to see what was reported and whether it went out.
import { Trans, useLingui } from "@lingui/react/macro"
import { getPagePath } from "@nanostores/router"
import { useStore } from "@nanostores/react"
import { memo, useEffect, useMemo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { $router, Link } from "@/components/router"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { $systems } from "@/lib/stores"
import type { MaintenanceAlert } from "@/types"

const SEV_DOT: Record<string, string> = {
	crit: "bg-red-500",
	warn: "bg-yellow-500",
	info: "bg-foreground/40",
	ok: "bg-green-500",
}
const SEV_TEXT: Record<string, string> = {
	crit: "text-red-500",
	warn: "text-yellow-500",
	info: "text-foreground",
	ok: "text-green-500",
}

function when(ts?: number): string {
	if (!ts) return ""
	const d = new Date(ts * 1000)
	const now = Date.now()
	const diff = Math.round((now - d.getTime()) / 1000)
	if (diff >= 0 && diff < 86400) {
		const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
		if (diff < 3600) return rtf.format(-Math.round(diff / 60), "minute")
		return rtf.format(-Math.round(diff / 3600), "hour")
	}
	return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

function statusWord(a: MaintenanceAlert): { text: string; cls: string } {
	if (a.x) return { text: a.x, cls: "text-muted-foreground" }
	if (a.o) return { text: "sent", cls: "text-green-500" }
	return { text: "failed", cls: "text-red-500" }
}

export default memo(() => {
	const { t } = useLingui()
	const systems = useStore($systems)

	useEffect(() => {
		document.title = `${t`Alerts`} / Beszel`
	}, [t])

	const feed = useMemo(() => {
		const rows: { sysId: string; sys: string; a: MaintenanceAlert }[] = []
		for (const s of systems) {
			for (const a of s.info?.mt?.r ?? []) {
				rows.push({ sysId: s.id, sys: s.name || s.info?.h || s.id, a })
			}
		}
		rows.sort((x, y) => (y.a.t ?? 0) - (x.a.t ?? 0))
		return rows.slice(0, 120)
	}, [systems])

	return (
		<>
			<div className="flex items-baseline justify-between gap-4 mb-4">
				<div>
					<h1 className="text-2xl font-semibold mb-1">
						<Trans>Alerts</Trans>
					</h1>
					<p className="text-sm text-muted-foreground">
						<Trans>Everything the maintenance engine reported, newest first.</Trans>
					</p>
				</div>
			</div>
			{feed.length === 0 ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>No alerts reported yet.</Trans>
				</div>
			) : (
				<Table>
					<TableHeader className="sticky top-0 z-10 bg-table-header">
						<TableRow>
							<TableHead className="w-28">
								<Trans>When</Trans>
							</TableHead>
							<TableHead className="w-24">
								<Trans>Severity</Trans>
							</TableHead>
							<TableHead className="w-28">
								<Trans>Kind</Trans>
							</TableHead>
							<TableHead>
								<Trans>Title</Trans>
							</TableHead>
							<TableHead className="w-40">
								<Trans>System</Trans>
							</TableHead>
							<TableHead className="w-40">
								<Trans>Delivery</Trans>
							</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{feed.map(({ sysId, sys, a }, i) => {
							const st = statusWord(a)
							return (
								<TableRow key={`${sysId}-${a.t}-${i}`}>
									<TableCell className="tabular-nums text-muted-foreground whitespace-nowrap">
										{when(a.t)}
									</TableCell>
									<TableCell>
										<span className="flex items-center gap-1.5">
											<span className={`block size-2 rounded-full ${SEV_DOT[a.s ?? ""] ?? "bg-foreground/40"}`} />
											<span className={SEV_TEXT[a.s ?? ""] ?? ""}>{a.s ?? "info"}</span>
										</span>
									</TableCell>
									<TableCell className="text-muted-foreground">{a.k ?? ""}</TableCell>
									<TableCell className="font-medium">
										{a.n || ""}
										{a.d ? <span className="block text-xs text-muted-foreground truncate max-w-md">{a.d}</span> : null}
									</TableCell>
									<TableCell>
										<Link href={getPagePath($router, "system", { id: sysId })} className="hover:underline">
											{sys}
										</Link>
									</TableCell>
									<TableCell className={st.cls}>{st.text}</TableCell>
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
