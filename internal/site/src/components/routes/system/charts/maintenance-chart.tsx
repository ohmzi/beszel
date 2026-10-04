import { t } from "@lingui/core/macro"
import type { ChartData } from "@/types"
import { ChartCard } from "../chart-card"
import LineChartDefault from "@/components/charts/line-chart"
import { toFixedFloat } from "@/lib/utils"

// homelab-maint verdict over time (Ohmz fork): 0 ok, 1 warn, 2 crit.
export function MaintenanceChart({ chartData, grid, dataEmpty }: { chartData: ChartData; grid: boolean; dataEmpty: boolean }) {
	return (
		<ChartCard
			empty={dataEmpty}
			grid={grid}
			title={t`Maintenance`}
			description={t`homelab-maint verdict over time (0 healthy, 1 attention, 2 critical)`}
			legend={false}
		>
			<LineChartDefault
				chartData={chartData}
				contentFormatter={(item) => String(item.value)}
				tickFormatter={(value) => String(toFixedFloat(value, 0))}
				legend={false}
				dataPoints={[
					{
						label: t`Maintenance`,
						color: "#e0913f",
						dataKey: ({ stats }) => stats?.mtl,
					},
				]}
			></LineChartDefault>
		</ChartCard>
	)
}
