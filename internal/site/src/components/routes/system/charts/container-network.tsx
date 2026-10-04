import { Trans } from "@lingui/react/macro"
import { useEffect, useState } from "react"
import { pb } from "@/lib/api"
import { formatBytes } from "@/lib/utils"

// Ohmz fork: per-container network usage. Beszel stores container_stats as a list of
// {n: name, b: [sent, recv]} (bytes/second), at the same retention tiers as system stats
// (20m = 24h, 120m = 7d, 480m = 30d). "Now" is the newest 1m sample.
const TIERS = [
	{ type: "480m", secs: 480 * 60 },
	{ type: "120m", secs: 120 * 60 },
	{ type: "20m", secs: 20 * 60 },
	{ type: "10m", secs: 10 * 60 },
	{ type: "1m", secs: 60 },
] as const

type Acc = { up: number; down: number; n: number }
type Row = { name: string; nowUp: number; nowDown: number; d24Up: number; d24Down: number; d7Up: number; d7Down: number }

function fmt(x: number): string {
	const { value, unit } = formatBytes(x)
	return `${value >= 100 ? Math.round(value).toLocaleString() : value.toFixed(1)} ${unit}`
}
const rate = (x: number) => `${fmt(x)}/s`

export function ContainerNetwork({ systemId }: { systemId: string }) {
	const [rows, setRows] = useState<Row[] | null>(null)

	useEffect(() => {
		let live = true
		const fetchTier = (type: string) =>
			pb
				.send<{ items: { stats?: { n: string; b?: [number, number] }[] }[] }>(
					`/api/collections/container_stats/records?perPage=1000&fields=stats&filter=${encodeURIComponent(`system="${systemId}" && type="${type}"`)}`,
					{ method: "GET" }
				)
				.catch(() => ({ items: [] as { stats?: { n: string; b?: [number, number] }[] }[] }))
		const latest = () =>
			pb
				.send<{ items: { stats?: { n: string; b?: [number, number] }[] }[] }>(
					`/api/collections/container_stats/records?perPage=1&fields=stats&sort=-created&filter=${encodeURIComponent(`system="${systemId}" && type="1m"`)}`,
					{ method: "GET" }
				)
				.catch(() => ({ items: [] as { stats?: { n: string; b?: [number, number] }[] }[] }))
		Promise.all([...TIERS.map((tr) => fetchTier(tr.type)), latest()]).then((res) => {
			if (!live) return
			const byTier = TIERS.map((tr, i) => {
				const acc = new Map<string, Acc>()
				for (const rec of res[i].items ?? []) {
					for (const c of rec.stats ?? []) {
						const b = c.b
						if (!Array.isArray(b)) continue
						const a = acc.get(c.n) ?? { up: 0, down: 0, n: 0 }
						a.up += (b[0] || 0) * tr.secs
						a.down += (b[1] || 0) * tr.secs
						acc.set(c.n, a)
					}
				}
				return acc
			})
			// since = coarsest tier with data; fall back through the chain for a fresh install
			const since = byTier.find((m) => m.size > 0) ?? new Map<string, Acc>()
			const week = byTier[1] // 120m
			const day = byTier[2].size ? byTier[2] : byTier[3].size ? byTier[3] : byTier[4]
			const now = new Map<string, [number, number]>()
			for (const c of res[TIERS.length].items?.[0]?.stats ?? []) {
				if (Array.isArray(c.b)) now.set(c.n, c.b)
			}
			const names = new Set<string>([...since.keys(), ...day.keys(), ...now.keys()])
			const out: Row[] = []
			for (const name of names) {
				const s = since.get(name) ?? { up: 0, down: 0, n: 0 }
				const w = week.get(name) ?? { up: 0, down: 0, n: 0 }
				const d = day.get(name) ?? { up: 0, down: 0, n: 0 }
				const nw = now.get(name) ?? [0, 0]
				if (s.up + s.down + w.up + w.down + d.up + d.down + nw[0] + nw[1] === 0) continue
				out.push({ name, nowUp: nw[0], nowDown: nw[1], d24Up: d.up, d24Down: d.down, d7Up: w.up, d7Down: w.down })
			}
			out.sort((a, b) => b.d24Down + b.d24Up - (a.d24Down + a.d24Up))
			setRows(out.slice(0, 40))
		})
		return () => {
			live = false
		}
	}, [systemId])

	return (
		<div className="rounded-lg border border-border bg-card p-4 xl:col-span-2">
			<h3 className="text-sm font-semibold mb-0.5">
				<Trans>Container network</Trans>
			</h3>
			<p className="text-xs text-muted-foreground mb-3">
				<Trans>Per-container upload and download: now, last 24 hours, last 7 days.</Trans>
			</p>
			{!rows?.length ? (
				<p className="text-sm text-muted-foreground py-4 text-center">
					<Trans>No container traffic recorded yet.</Trans>
				</p>
			) : (
				<div className="overflow-x-auto">
					<table className="w-full text-sm tabular-nums">
						<thead>
							<tr className="text-xs text-muted-foreground">
								<th className="text-left font-medium py-1">
									<Trans>Container</Trans>
								</th>
								<th className="text-right font-medium py-1">
									<Trans>Now (↓ / ↑)</Trans>
								</th>
								<th className="text-right font-medium py-1">
									<Trans>24h (↓ / ↑)</Trans>
								</th>
								<th className="text-right font-medium py-1">
									<Trans>7d (↓ / ↑)</Trans>
								</th>
							</tr>
						</thead>
						<tbody>
							{rows.map((r) => (
								<tr key={r.name} className="border-t border-border/50">
									<td className="py-1 truncate max-w-48">{r.name}</td>
									<td className="py-1 text-right whitespace-nowrap">
										<span className="text-green-500">↓</span> {rate(r.nowDown)} <span className="text-blue-500">↑</span> {rate(r.nowUp)}
									</td>
									<td className="py-1 text-right whitespace-nowrap">
										<span className="text-green-500">↓</span> {fmt(r.d24Down)} <span className="text-blue-500">↑</span> {fmt(r.d24Up)}
									</td>
									<td className="py-1 text-right whitespace-nowrap text-muted-foreground">
										<span className="text-green-500">↓</span> {fmt(r.d7Down)} <span className="text-blue-500">↑</span> {fmt(r.d7Up)}
									</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			)}
		</div>
	)
}
