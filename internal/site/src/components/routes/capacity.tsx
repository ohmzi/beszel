import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { bytes, dotFor, useMaintenanceFile } from "@/lib/maintenance"

type Mount = {
	mount?: string
	used_pct?: number
	free_b?: number
	size_b?: number
	free_h?: string
	days?: number | null
	level?: string
	info?: boolean
}
type Freed = { day?: string; bytes?: number }

export default memo(() => {
	const s = useMaintenanceFile<{ mounts?: Mount[]; freed_by_day?: Freed[] }>("storage.json")
	const mounts = (s?.mounts ?? []).filter((m) => !m.info)
	const soon = mounts
		.filter((m) => typeof m.days === "number" && m.days >= 0)
		.sort((a, b) => (a.days ?? 0) - (b.days ?? 0))
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Capacity</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Storage headroom and how soon each mount fills at the current rate.</Trans>
				</p>
			</div>

			{soon.length ? (
				<div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
					{soon.slice(0, 4).map((m) => (
						<div key={m.mount} className="rounded-lg border border-border bg-card p-3">
							<div className="text-xs text-muted-foreground truncate">{m.mount}</div>
							<div className="text-xl font-semibold tabular-nums">
								{typeof m.days === "number" ? `${Math.round(m.days)} d` : "-"}
							</div>
							<div className="text-xs text-muted-foreground">until full · {Math.round(m.used_pct ?? 0)}% used</div>
						</div>
					))}
				</div>
			) : null}

			<Table>
				<TableHeader className="sticky top-0 z-10 bg-table-header">
					<TableRow>
						<TableHead>
							<Trans>Mount</Trans>
						</TableHead>
						<TableHead className="w-24 text-right">
							<Trans>Used</Trans>
						</TableHead>
						<TableHead className="w-28 text-right">
							<Trans>Free</Trans>
						</TableHead>
						<TableHead className="w-28 text-right">
							<Trans>Size</Trans>
						</TableHead>
						<TableHead className="w-28 text-right">
							<Trans>Full in</Trans>
						</TableHead>
						<TableHead className="w-20">
							<Trans>Status</Trans>
						</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{mounts.map((m) => (
						<TableRow key={m.mount}>
							<TableCell className="font-mono text-xs">{m.mount}</TableCell>
							<TableCell className="text-right tabular-nums">{Math.round(m.used_pct ?? 0)}%</TableCell>
							<TableCell className="text-right tabular-nums">{m.free_h || bytes(m.free_b)}</TableCell>
							<TableCell className="text-right tabular-nums text-muted-foreground">{bytes(m.size_b)}</TableCell>
							<TableCell className="text-right tabular-nums">
								{typeof m.days === "number" && m.days >= 0
									? `${Math.round(m.days)} d`
									: m.days == null
										? "-"
										: "over a year"}
							</TableCell>
							<TableCell>
								<span className={`block size-2 rounded-full ${dotFor(m.level)}`} />
							</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>
			<FooterRepoLink />
		</>
	)
})
