import { useLingui } from "@lingui/react/macro"
import { memo, useEffect } from "react"
import NetworkMonitorsTableNew from "@/components/network-monitors-table/network-monitors-table"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { NetworkTotals } from "@/components/routes/system/charts/network-totals"
import { useNetworkMonitors } from "@/lib/use-network-monitors"
import { $allSystemsById } from "@/lib/stores"
import { supportsNetworkMonitors } from "@/lib/utils"
import { useStore } from "@nanostores/react"

export default memo(() => {
	const { t } = useLingui()
	const { monitors, isLoading } = useNetworkMonitors({})
	const systems = useStore($allSystemsById)
	const visibleMonitors = monitors.filter((monitor) => {
		const system = systems[monitor.system]
		return !system || supportsNetworkMonitors(system)
	})
	// Ohmz fork: bandwidth lives per container/system, not per monitor; show it here so the
	// monitors page answers "what is each service moving, now and over time".
	const sysId = Object.keys(systems)[0] ?? ""

	useEffect(() => {
		document.title = "OhmzMaintainer"
	}, [t])

	return (
		<>
			{sysId ? (
				<div className="mb-6">
					<NetworkTotals systemId={sysId} />
				</div>
			) : null}
			<NetworkMonitorsTableNew monitors={visibleMonitors} isLoading={isLoading} />
			<FooterRepoLink />
		</>
	)
})
