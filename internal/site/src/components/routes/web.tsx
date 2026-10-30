// home.ohmz.cloud access log (Ohmz fork). The engine publishes web.json from the origin nginx log, enriched with DB-IP
// Lite city/ASN, a coarse User-Agent parse and the network organization. Server-side only: no client tracker, no
// cookie, no fingerprint. A network organization is the IP owner, not a person; e-mail scanners can appear here too.
import { Trans } from "@lingui/react/macro"
import { memo, useEffect } from "react"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useMaintenanceFile, when } from "@/lib/maintenance"

type Row = {
	ts: number
	ip: string
	country?: string
	region?: string
	city?: string
	asn?: number
	org?: string
	device?: string
	browser?: string
	os?: string
	path?: string
	referer?: string
	status?: number
	bytes?: number
}

type WebFile = {
	available?: boolean
	generated?: number
	rows?: Row[]
	window?: { from?: number | null; to?: number | null }
	sources?: Record<string, { status?: string; error?: string | null }>
	note?: string
	attribution?: string
}

function time(ts?: number): string {
	if (!ts) return "-"
	return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

function Stat({ label, value }: { label: string; value: string | number }) {
	return (
		<div className="rounded-lg border border-border bg-card px-4 py-3">
			<div className="text-xs text-muted-foreground">{label}</div>
			<div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
		</div>
	)
}

export default memo(() => {
	const web = useMaintenanceFile<WebFile>("web.json")

	useEffect(() => {
		document.title = "OhmzMaintainer"
	}, [])

	const rows = web?.rows ?? []
	const unique = new Set(rows.map((r) => r.ip)).size
	const networks = new Set(rows.map((r) => r.org).filter(Boolean)).size
	const error = web?.sources?.nginx?.error

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Website</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>
						home.ohmz.cloud access, newest first. Server-side nginx logs only; no client-side tracker or cookie.
					</Trans>
				</p>
			</div>

			{web === null ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>Loading the website log…</Trans>
				</div>
			) : web.available === false ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					{error ? (
						<Trans>No website log yet. {error}</Trans>
					) : (
						<Trans>No public requests have reached the origin yet.</Trans>
					)}
				</div>
			) : (
				<>
					<div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
						<Stat label="Requests" value={rows.length} />
						<Stat label="Unique addresses" value={unique} />
						<Stat label="Unique networks" value={networks} />
						<Stat label="Last published" value={when(web.generated)} />
					</div>

					<div className="overflow-x-auto rounded-lg border border-border bg-card">
						<Table stack>
							<TableHeader className="sticky top-0 z-10 bg-table-header">
								<TableRow>
									<TableHead className="w-40">
										<Trans>Time</Trans>
									</TableHead>
									<TableHead>
										<Trans>Address</Trans>
									</TableHead>
									<TableHead>
										<Trans>Location</Trans>
									</TableHead>
									<TableHead>
										<Trans>Network</Trans>
									</TableHead>
									<TableHead>
										<Trans>Device</Trans>
									</TableHead>
									<TableHead>
										<Trans>Path</Trans>
									</TableHead>
									<TableHead className="w-20 text-right">
										<Trans>Status</Trans>
									</TableHead>
								</TableRow>
							</TableHeader>
							<TableBody>
								{rows.map((r, i) => (
									<TableRow key={`${r.ts}-${r.ip}-${i}`}>
										<TableCell className="whitespace-nowrap text-xs text-muted-foreground">{time(r.ts)}</TableCell>
										<TableCell className="font-mono text-xs">{r.ip}</TableCell>
										<TableCell className="text-xs">
											<div>{r.country || "-"}</div>
											<div className="text-muted-foreground">{r.city || r.region || ""}</div>
										</TableCell>
										<TableCell className="text-xs">
											<div className="font-medium">{r.org || "-"}</div>
											<div className="text-muted-foreground">{r.asn ? `AS${r.asn}` : ""}</div>
										</TableCell>
										<TableCell className="text-xs">
											<div>
												{r.browser || "-"} / {r.os || "-"}
											</div>
											<div className="text-muted-foreground">{r.device || ""}</div>
										</TableCell>
										<TableCell className="text-xs">
											<div className="max-w-[340px] truncate font-mono" title={r.path}>
												{r.path || "-"}
											</div>
											{r.referer && (
												<div className="max-w-[340px] truncate text-[10px] text-muted-foreground" title={r.referer}>
													{r.referer}
												</div>
											)}
										</TableCell>
										<TableCell className="text-right">
											<Badge variant={r.status && r.status >= 400 ? "danger" : "secondary"}>{r.status ?? "-"}</Badge>
										</TableCell>
									</TableRow>
								))}
							</TableBody>
						</Table>
					</div>

					<p className="mt-4 max-w-4xl text-xs text-muted-foreground">
						{web.note} {web.attribution && <span>{web.attribution}</span>}
					</p>
				</>
			)}
		</>
	)
})
