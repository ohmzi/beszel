import { Trans } from "@lingui/react/macro"
import { memo } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { dotFor, useMaintenanceFile } from "@/lib/maintenance"

type Item = {
	name?: string
	title?: string
	mode?: string
	kind?: string
	state?: string
	replaced_by?: string
	via?: string
	retirable?: boolean
	blocked_by?: string[]
}

const tile = (label: string, value: string) => (
	<div className="rounded-lg border border-border bg-card p-3">
		<div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
		<div className="text-xl font-semibold tabular-nums">{value}</div>
	</div>
)

export default memo(() => {
	const m = useMaintenanceFile<{
		total?: number
		retirable?: number
		retired?: number
		remaining?: number
		complete?: boolean
		paused?: boolean
		items?: Item[]
	}>("migration.json")
	const items = [...(m?.items ?? [])].sort((a, b) => (a.state === "retired" ? 1 : 0) - (b.state === "retired" ? 1 : 0))
	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Migration</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Legacy units retired one at a time; the rest observed until they are cut over.</Trans>
				</p>
			</div>
			<div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
				{tile("Legacy units", String(m?.total ?? 0))}
				{tile("Retired", String(m?.retired ?? 0))}
				{tile("Remaining", String(m?.remaining ?? 0))}
				{tile("Retirable now", String(m?.retirable ?? 0))}
			</div>
			<Table>
				<TableHeader className="sticky top-0 z-10 bg-table-header">
					<TableRow>
						<TableHead>
							<Trans>Unit</Trans>
						</TableHead>
						<TableHead className="w-24">
							<Trans>Kind</Trans>
						</TableHead>
						<TableHead className="w-28">
							<Trans>Mode</Trans>
						</TableHead>
						<TableHead className="w-32">
							<Trans>State</Trans>
						</TableHead>
						<TableHead className="w-48">
							<Trans>Replaced by</Trans>
						</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{items.map((it) => (
						<TableRow key={it.name}>
							<TableCell>
								<span className="font-medium">{it.title || it.name}</span>
								<span className="block text-xs text-muted-foreground font-mono">{it.name}</span>
							</TableCell>
							<TableCell className="text-muted-foreground">{it.kind ?? ""}</TableCell>
							<TableCell className="text-muted-foreground">{it.mode ?? ""}</TableCell>
							<TableCell>
								<span className="flex items-center gap-1.5">
									<span
										className={`block size-2 rounded-full ${dotFor(it.state === "retired" ? "retired" : it.state === "pending" ? "pending" : it.mode)}`}
									/>
									{it.state ?? ""}
								</span>
							</TableCell>
							<TableCell className="text-muted-foreground font-mono text-xs">
								{it.replaced_by || it.via || ""}
							</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>
			<FooterRepoLink />
		</>
	)
})
