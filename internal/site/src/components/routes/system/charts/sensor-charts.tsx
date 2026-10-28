import { t } from "@lingui/core/macro"
import AreaChartDefault from "@/components/charts/area-chart"
import { batteryStateTranslations } from "@/lib/i18n"
import { $fanFilter, $temperatureFilter, $userSettings } from "@/lib/stores"
import { cn, decimalString, formatTemperature, toFixedFloat } from "@/lib/utils"
import type { ChartData, SystemRecord, SystemStatsRecord } from "@/types"
import { ChartCard, FilterBar } from "../chart-card"
import LineChartDefault from "@/components/charts/line-chart"
import type { DataPoint } from "@/components/charts/line-chart"
import { useStore } from "@nanostores/react"
import { useRef, useMemo, useState, useEffect } from "react"

export function BatteryChart({
	chartData,
	grid,
	dataEmpty,
	maxValues,
	system,
}: {
	chartData: ChartData
	grid: boolean
	dataEmpty: boolean
	maxValues: boolean
	system: SystemRecord
}) {
	const batteryNames = useMemo(() => {
		const names = new Set<string>()
		for (const record of chartData.systemStats) {
			for (const name in record.stats?.bats ?? {}) {
				names.add(name)
			}
		}
		return [...names].sort()
	}, [chartData.systemStats])
	const hasNamedBatteries = batteryNames.length > 0
	const showBatteryChart = hasNamedBatteries || chartData.systemStats.some((record) => record.stats?.bat)

	if (!showBatteryChart) {
		return null
	}

	if (hasNamedBatteries) {
		const dataPoints = batteryNames.map((name, index) => ({
			label: name,
			dataKey: ({ stats }: SystemStatsRecord) => stats?.bats?.[name],
			color: `hsl(${(index * 360 + 226) / batteryNames.length}, 65%, 52%)`,
		}))
		return (
			<ChartCard
				empty={dataEmpty}
				grid={grid}
				title={t`Battery`}
				description={`${t({
					message: "Current state",
					comment: "Context: Battery state",
				})}: ${batteryStateTranslations[system.info.bat?.[1] ?? 0]()}`}
			>
				<LineChartDefault
					chartData={chartData}
					maxToggled={maxValues}
					dataPoints={dataPoints}
					domain={[0, 100]}
					legend={true}
					tickFormatter={(val) => `${val}%`}
					contentFormatter={({ value }) => `${value}%`}
					itemSorter={(a, b) => b.value - a.value}
				/>
			</ChartCard>
		)
	}

	return (
		<ChartCard
			empty={dataEmpty}
			grid={grid}
			title={t`Battery`}
			description={`${t({
				message: "Current state",
				comment: "Context: Battery state",
			})}: ${batteryStateTranslations[system.info.bat?.[1] ?? 0]()}`}
		>
			<AreaChartDefault
				chartData={chartData}
				maxToggled={maxValues}
				dataPoints={[
					{
						label: t`Charge`,
						dataKey: ({ stats }) => stats?.bat?.[0],
						color: 1,
						opacity: 0.35,
					},
				]}
				domain={[0, 100]}
				tickFormatter={(val) => `${val}%`}
				contentFormatter={({ value }) => `${value}%`}
			/>
		</ChartCard>
	)
}

// Temperature sensors arrive one key per sensor: every P-core, every disk, every aux channel.
// Group them by what they actually measure so the chart shows a handful of meaningful lines.
// The group's value is the hottest member — that is the one that matters.
const TEMP_GROUPS: { label: string; re: RegExp }[] = [
	// The board's PECI channel is named nct6798_peci_agent_0, so `peci_agent` cannot be anchored at the start.
	{ label: "CPU", re: /(^coretemp|^k10temp|^zenpower|peci_agent|^cpu_thermal)/i },
	{ label: "GPU", re: /(geforce|nvidia|rtx|radeon|^amd |arc a|apple gpu)/i },
	{ label: "NVMe", re: /^nvme/i },
	{ label: "HDD", re: /^(drivetemp|sd[a-z]$)/i },
	{ label: "RAM", re: /^(spd5|jc42|dimmtemp)/i },
	{ label: "Board", re: /(systin|^acpitz$)/i },
]
// Bogus or irrelevant channels: Super-I/O aux inputs, PECI calibration, Wi-Fi.
const TEMP_IGNORE = /(calibration|auxtin|cputin|iwlwifi)/i

function tempGroup(key: string): string | null {
	if (TEMP_IGNORE.test(key)) return null
	for (const g of TEMP_GROUPS) if (g.re.test(key)) return g.label
	return null
}

export function TemperatureChart({
	chartData,
	grid,
	dataEmpty,
	setPageBottomExtraMargin,
}: {
	chartData: ChartData
	grid: boolean
	dataEmpty: boolean
	setPageBottomExtraMargin?: (margin: number) => void
}) {
	const showTempChart = chartData.systemStats.at(-1)?.stats.t

	const filter = useStore($temperatureFilter)
	const userSettings = useStore($userSettings)

	const statsRef = useRef(chartData.systemStats)
	statsRef.current = chartData.systemStats

	// Derive sensor names key from latest data point
	let sensorNamesKey = ""
	for (let i = chartData.systemStats.length - 1; i >= 0; i--) {
		const t = chartData.systemStats[i].stats?.t
		if (t) {
			sensorNamesKey = Object.keys(t).sort().join("\0")
			break
		}
	}

	// Only recompute colors and dataKey functions when sensor names change
	const { colorMap, dataKeys, sortedKeys } = useMemo(() => {
		const stats = statsRef.current
		const sums = {} as Record<string, number>
		for (const data of stats) {
			const t = data.stats?.t
			if (!t) continue
			for (const key of Object.keys(t)) {
				const group = tempGroup(key)
				if (group) sums[group] = (sums[group] ?? 0) + t[key]
			}
		}
		const sorted = Object.keys(sums).sort((a, b) => sums[b] - sums[a])
		const colorMap = {} as Record<string, string>
		const dataKeys = {} as Record<string, (d: SystemStatsRecord) => number | undefined>
		for (let i = 0; i < sorted.length; i++) {
			const label = sorted[i]
			colorMap[label] = `hsl(${((i * 360) / sorted.length) % 360}, 60%, 55%)`
			dataKeys[label] = (d: SystemStatsRecord) => {
				const t = d.stats?.t
				if (!t) return undefined
				let max: number | undefined
				for (const key of Object.keys(t)) {
					if (tempGroup(key) === label && (max === undefined || t[key] > max)) max = t[key]
				}
				return max
			}
		}
		return { colorMap, dataKeys, sortedKeys: sorted }
	}, [sensorNamesKey])

	const dataPoints = useMemo(() => {
		return sortedKeys.map((key) => {
			const filterTerms = filter
				? filter
						.toLowerCase()
						.split(" ")
						.filter((term) => term.length > 0)
				: []
			const filtered = filterTerms.length > 0 && !filterTerms.some((term) => key.toLowerCase().includes(term))
			const strokeOpacity = filtered ? 0.1 : 1
			return {
				label: key,
				dataKey: dataKeys[key],
				color: colorMap[key],
				strokeOpacity,
				activeDot: !filtered,
			}
		})
	}, [sortedKeys, filter, dataKeys, colorMap])

	// test with lots of data points
	// const totalPoints = 50
	// if (dataPoints.length > 0 && dataPoints.length < totalPoints) {
	// 	let i = 0
	// 	while (dataPoints.length < totalPoints) {
	// 		dataPoints.push({
	// 			label: `Test ${++i}`,
	// 			dataKey: () => 0,
	// 			color: "red",
	// 			strokeOpacity: 1,
	// 		})
	// 	}
	// }

	const chartRef = useRef<HTMLDivElement>(null)
	const [addMargin, setAddMargin] = useState(false)
	const marginPx = (dataPoints.length - 13) * 18

	useEffect(() => {
		if (setPageBottomExtraMargin && dataPoints.length > 13 && chartRef.current) {
			const checkPosition = () => {
				if (!chartRef.current) return
				const rect = chartRef.current.getBoundingClientRect()
				const actualScrollHeight = addMargin
					? document.documentElement.scrollHeight - marginPx
					: document.documentElement.scrollHeight
				const distanceToBottom = actualScrollHeight - (rect.bottom + window.scrollY)

				if (distanceToBottom < 250) {
					setAddMargin(true)
					setPageBottomExtraMargin(marginPx)
				} else {
					setAddMargin(false)
					setPageBottomExtraMargin(0)
				}
			}
			checkPosition()
			const timer = setTimeout(checkPosition, 500)
			return () => {
				clearTimeout(timer)
			}
		} else if (addMargin) {
			setAddMargin(false)
			if (setPageBottomExtraMargin) setPageBottomExtraMargin(0)
		}
	}, [dataPoints.length, addMargin, marginPx, setPageBottomExtraMargin])

	if (!showTempChart) {
		return null
	}

	const legend = dataPoints.length < 12

	return (
		<div ref={chartRef} className={cn("odd:last-of-type:col-span-full", { "col-span-full": !grid })}>
			<ChartCard
				empty={dataEmpty}
				grid={grid}
				title={t`Temperature`}
				description={t`Temperatures of system sensors`}
				cornerEl={<FilterBar store={$temperatureFilter} />}
				legend={legend}
			>
				<LineChartDefault
					chartData={chartData}
					itemSorter={(a, b) => b.value - a.value}
					domain={["auto", "auto"]}
					legend={legend}
					tickFormatter={(val) => {
						const { value, unit } = formatTemperature(val, userSettings.unitTemp)
						return `${toFixedFloat(value, 2)} ${unit}`
					}}
					contentFormatter={(item) => {
						const { value, unit } = formatTemperature(item.value, userSettings.unitTemp)
						return `${decimalString(value)} ${unit}`
					}}
					dataPoints={dataPoints}
					filter={filter}
				></LineChartDefault>
			</ChartCard>
		</div>
	)
}

// The agent names a fan "<chip>_<label-or-fan-idx>". This board (ASUS TUF B760M-PLUS WIFI,
// nct6798) exposes no fan*_label files, so map the headers to the case's physical fans
// instead of showing raw keys. The mapping was measured on 2026-10-27; see the fan-zone
// notes in the maintenance engine. "Unused" entries are dropped from the chart.
const FAN_LABELS: Record<string, string> = {
	nct6798_fan1: "Rear exhaust",
	nct6798_fan2: "Radiator",
	nct6798_fan4: "Front top",
	nct6798_fan5: "Front bottom",
	nct6798_fan6: "AIO pump",
	nct6798_fan3: "Unused",
	nct6798_fan7: "Unused",
}

function fanLabel(key: string): string {
	return FAN_LABELS[key] ?? key
}

export function FanChart({ chartData, grid, dataEmpty }: { chartData: ChartData; grid: boolean; dataEmpty: boolean }) {
	const showFanChart = chartData.systemStats.at(-1)?.stats.f

	const filter = useStore($fanFilter)

	const statsRef = useRef(chartData.systemStats)
	statsRef.current = chartData.systemStats

	// Stable key derived from current sensor names (sorted) so the memo only
	// recomputes when the set of fans changes — not on every data point.
	let sensorNamesKey = ""
	for (let i = chartData.systemStats.length - 1; i >= 0; i--) {
		const f = chartData.systemStats[i].stats?.f
		if (f) {
			sensorNamesKey = Object.keys(f).sort().join("\0")
			break
		}
	}

	const { colorMap, dataKeys, sortedKeys } = useMemo(() => {
		const stats = statsRef.current
		const sums = {} as Record<string, number>
		for (const data of stats) {
			const f = data.stats?.f
			if (!f) continue
			for (const key of Object.keys(f)) {
				sums[key] = (sums[key] ?? 0) + f[key]
			}
		}
		const sorted = Object.keys(sums)
			.sort((a, b) => sums[b] - sums[a])
			.filter((key) => fanLabel(key) !== "Unused")
		const colorMap = {} as Record<string, string>
		const dataKeys = {} as Record<string, (d: SystemStatsRecord) => number | undefined>
		for (let i = 0; i < sorted.length; i++) {
			const key = sorted[i]
			colorMap[key] = `hsl(${((i * 360) / sorted.length) % 360}, 60%, 55%)`
			dataKeys[key] = (d: SystemStatsRecord) => d.stats?.f?.[key]
		}
		return { colorMap, dataKeys, sortedKeys: sorted }
	}, [sensorNamesKey])

	const dataPoints = useMemo(() => {
		return sortedKeys.map((key) => {
			const filterTerms = filter
				? filter
						.toLowerCase()
						.split(" ")
						.filter((term) => term.length > 0)
				: []
			const label = fanLabel(key)
			const filtered = filterTerms.length > 0 && !filterTerms.some((term) => label.toLowerCase().includes(term) || key.toLowerCase().includes(term))
			const strokeOpacity = filtered ? 0.1 : 1
			return {
				label,
				dataKey: dataKeys[key],
				color: colorMap[key],
				strokeOpacity,
				activeDot: !filtered,
			}
		})
	}, [sortedKeys, filter, dataKeys, colorMap])

	// GPU fans report a duty percentage, not RPM: their driver exposes no tachometer. Plot the
	// hottest GPU fan on the right-hand % axis so the RPM scale stays honest.
	const gpuFanPoint = useMemo((): DataPoint | null => {
		const gpus = chartData.systemStats.at(-1)?.stats?.g
		if (!gpus || !Object.values(gpus).some((g) => g?.fs !== undefined)) return null
		const terms = filter ? filter.toLowerCase().split(" ").filter((term) => term.length > 0) : []
		const filtered = terms.length > 0 && !terms.some((term) => "gpu fan".includes(term))
		return {
			label: "GPU fan",
			dataKey: (d: SystemStatsRecord) => {
				const map = d.stats?.g
				if (!map) return undefined
				let max: number | undefined
				for (const g of Object.values(map)) {
					if (g?.fs !== undefined && (max === undefined || g.fs > max)) max = g.fs
				}
				return max
			},
			color: "#f59e0b",
			strokeOpacity: filtered ? 0.1 : 1,
			activeDot: !filtered,
			yAxisId: "right",
			strokeDasharray: "4 3",
		}
	}, [chartData.systemStats, filter])

	if (!showFanChart) {
		return null
	}

	const allPoints = gpuFanPoint ? [...dataPoints, gpuFanPoint] : dataPoints
	const legend = allPoints.length < 12

	return (
		<div className={cn("odd:last-of-type:col-span-full", { "col-span-full": !grid })}>
			<ChartCard
				empty={dataEmpty}
				grid={grid}
				title={t`Fans`}
				description={t`System fan speeds (RPM; GPU fan duty on the right axis)`}
				cornerEl={<FilterBar store={$fanFilter} />}
				legend={legend}
			>
				<LineChartDefault
					chartData={chartData}
					itemSorter={(a, b) => b.value - a.value}
					domain={["auto", "auto"]}
					domain2={[0, 100]}
					legend={legend}
					tickFormatter={(val) => `${toFixedFloat(val, 0)}`}
					tickFormatter2={(val) => `${toFixedFloat(val, 0)}%`}
					contentFormatter={(item) => item?.name === "GPU fan" ? `${decimalString(item.value, 0)}%` : `${decimalString(item.value, 0)} RPM`}
					dataPoints={allPoints}
					filter={filter}
				></LineChartDefault>
			</ChartCard>
		</div>
	)
}
