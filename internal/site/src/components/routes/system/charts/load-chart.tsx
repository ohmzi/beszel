import { t } from "@lingui/core/macro"
import type { ChartData, GPUData } from "@/types"
import { ChartCard } from "../chart-card"
import LineChartDefault from "@/components/charts/line-chart"
import { decimalString, toFixedFloat } from "@/lib/utils"

// CPU and GPU utilisation on one 0-100 axis: these are the two loads the fan curves
// actually chase -- CPU package temperature and GPU temperature follow from them.
export function SystemLoadChart({ chartData, grid, dataEmpty }: { chartData: ChartData; grid: boolean; dataEmpty: boolean }) {
	return (
		<ChartCard empty={dataEmpty} grid={grid} title={t`Load`} description={t`CPU and GPU utilization`}>
			<LineChartDefault
				chartData={chartData}
				domain={[0, 100]}
				tickFormatter={(value) => `${toFixedFloat(value, 0)}%`}
				contentFormatter={(item) => `${decimalString(item.value)}%`}
				legend={true}
				dataPoints={[
					{
						label: t`CPU`,
						color: "#3b82f6",
						dataKey: ({ stats }) => stats?.cpu,
					},
					{
						label: t`GPU`,
						color: "#f59e0b",
						dataKey: ({ stats }) => {
							const gpus = stats?.g as Record<string, GPUData | undefined> | undefined
							if (!gpus) return undefined
							let max: number | undefined
							for (const gpu of Object.values(gpus)) {
								const usage = gpu?.u
								if (usage !== undefined && (max === undefined || usage > max)) max = usage
							}
							return max
						},
					},
				]}
			/>
		</ChartCard>
	)
}
