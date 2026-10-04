// Ohmz fork: a tiny dependency-free sparkline used by the Live, Capacity and Health pages.
// Values render as a filled area line; a missing/empty series renders nothing.
import { memo } from "react"

export const Sparkline = memo(function Sparkline({
	values,
	width = 160,
	height = 34,
	min,
	max,
	className,
}: {
	values?: number[]
	width?: number
	height?: number
	min?: number
	max?: number
	className?: string
}) {
	const v = (values ?? []).filter((n) => typeof n === "number" && Number.isFinite(n))
	if (v.length < 2) return <div className={className} style={{ height }} />
	let lo = min ?? Math.min(...v)
	let hi = max ?? Math.max(...v)
	if (hi - lo < 1e-9) {
		hi = lo + 1
	}
	const px = (i: number) => (i / (v.length - 1)) * width
	const py = (n: number) => height - ((n - lo) / (hi - lo)) * (height - 2) - 1
	const line = v.map((n, i) => `${i ? "L" : "M"}${px(i).toFixed(1)} ${py(n).toFixed(1)}`).join(" ")
	const area = `${line} L${width} ${height} L0 ${height} Z`
	return (
		<svg
			width={width}
			height={height}
			viewBox={`0 0 ${width} ${height}`}
			className={className}
			preserveAspectRatio="none"
			aria-hidden="true"
		>
			<path d={area} fill="var(--color-primary)" opacity="0.12" />
			<path
				d={line}
				fill="none"
				stroke="var(--color-primary)"
				strokeWidth="1.5"
				strokeLinejoin="round"
				strokeLinecap="round"
			/>
			<circle cx={px(v.length - 1)} cy={py(v[v.length - 1])} r="1.8" fill="var(--color-primary)" />
		</svg>
	)
})
