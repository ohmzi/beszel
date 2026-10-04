import { t } from "@lingui/core/macro"
import { Trans } from "@lingui/react/macro"
import { useEffect, useState } from "react"
import { pb } from "@/lib/api"
import { formatBytes } from "@/lib/utils"

// Ohmz fork: total upload/download over the retained history. Each rollup tier stores a bytes/second
// rate averaged over its bucket, so a bucket's bytes = rate × bucket seconds.
// Retention: 20m = 24h, 120m = 7d, 480m = 30d (10m/1m are fallbacks for a fresh install).
const CHAIN = [
	{ type: "480m", secs: 480 * 60 },
	{ type: "120m", secs: 120 * 60 },
	{ type: "20m", secs: 20 * 60 },
	{ type: "10m", secs: 10 * 60 },
	{ type: "1m", secs: 60 },
] as const

type Sum = { n: number; up: number; down: number }

function fmt(x: number): string {
	const { value, unit } = formatBytes(x)
	const v = value >= 100 ? Math.round(value).toLocaleString() : value.toFixed(1)
	return `${v} ${unit}`
}

export function NetworkTotals({ systemId }: { systemId: string }) {
	const [rows, setRows] = useState<{ label: string; up: number; down: number }[] | null>(null)

	useEffect(() => {
		let live = true
		Promise.all(
			CHAIN.map((tr) =>
				pb
					.send<{ items: { stats?: { b?: [number, number] } }[] }>(
						`/api/collections/system_stats/records?perPage=1000&fields=stats&filter=${encodeURIComponent(`system="${systemId}" && type="${tr.type}"`)}`,
						{ method: "GET" }
					)
					.then((d): Sum => {
						let up = 0
						let down = 0
						for (const r of d.items ?? []) {
							const b = r.stats?.b
							if (Array.isArray(b)) {
								up += (b[0] || 0) * tr.secs
								down += (b[1] || 0) * tr.secs
							}
						}
						return { n: (d.items ?? []).length, up, down }
					})
					.catch((): Sum => ({ n: 0, up: 0, down: 0 }))
			)
		).then((sums) => {
			if (!live) return
			const byType = Object.fromEntries(CHAIN.map((tr, i) => [tr.type, sums[i]]))
			// "since data": the coarsest tier that actually has rows (a fresh install only has 1m/10m).
			const since = CHAIN.map((tr) => byType[tr.type]).find((s) => s.n > 0) ?? { n: 0, up: 0, down: 0 }
			const week = byType["120m"]
			const day = byType["20m"].n > 0 ? byType["20m"] : byType["10m"].n > 0 ? byType["10m"] : byType["1m"]
			setRows([
				{ label: t`Since data (up to 30 days)`, ...since },
				{ label: t`Last 7 days`, ...week },
				{ label: t`Last 24 hours`, ...day },
			])
		})
		return () => {
			live = false
		}
	}, [systemId])

	return (
		<div className="rounded-lg border border-border bg-card p-4">
			<h3 className="text-sm font-semibold mb-0.5">
				<Trans>Network totals</Trans>
			</h3>
			<p className="text-xs text-muted-foreground mb-3">
				<Trans>Upload and download over the retained history.</Trans>
			</p>
			<table className="w-full text-sm tabular-nums">
				<tbody>
					{(rows ?? [{ label: t`Since data (up to 30 days)`, up: 0, down: 0 }, { label: t`Last 7 days`, up: 0, down: 0 }, { label: t`Last 24 hours`, up: 0, down: 0 }]).map(
						(r, i) => {
							// A tier that has not rolled up yet (7d/30d on a fresh install) reads "collecting…",
							// not a bare 0 next to a non-zero day figure.
							const day = rows?.[2]
							const collecting = !!day && day.up + day.down > 0 && r.up + r.down === 0 && i < 2
							return (
								<tr key={r.label} className="border-t border-border/50">
									<td className="py-1.5 text-muted-foreground">{r.label}</td>
									{collecting ? (
										<td className="py-1.5 text-right text-muted-foreground" colSpan={2}>
											<Trans>collecting…</Trans>
										</td>
									) : (
										<>
											<td className="py-1.5 text-right">
												<span className="text-green-500">↓</span> {fmt(r.down)}
											</td>
											<td className="py-1.5 text-right">
												<span className="text-blue-500">↑</span> {fmt(r.up)}
											</td>
										</>
									)}
								</tr>
							)
						}
					)}
				</tbody>
			</table>
		</div>
	)
}
