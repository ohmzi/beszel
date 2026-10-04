// Services page (Ohmz fork): the host tables that have no dedicated page of their own —
// systemd services, pending package updates and ZFS pools. The home screen is graphs only.
import { Trans, useLingui } from "@lingui/react/macro"
import { useStore } from "@nanostores/react"
import { memo, useEffect } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { LazyPackageUpdatesTable, LazySystemdTable, LazyZfsTable } from "@/components/routes/system/lazy-tables"
import { $allSystemsById } from "@/lib/stores"

export default memo(() => {
	const { t } = useLingui()
	const systems = useStore($allSystemsById)
	const sysId = Object.keys(systems)[0] ?? ""
	const sys = systems[sysId]
	const counts = sys?.info?.pu?.[0] ? sys.info.pu.join(",") : ""

	useEffect(() => {
		document.title = "OhmzMaintainer"
	}, [t])

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Services</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>Systemd services, pending package updates and storage pools for this host.</Trans>
				</p>
			</div>
			{sysId ? (
				<div className="grid gap-4">
					<LazySystemdTable systemId={sysId} />
					{counts ? <LazyPackageUpdatesTable systemId={sysId} counts={counts} /> : null}
					<LazyZfsTable systemId={sysId} />
				</div>
			) : null}
			<FooterRepoLink />
		</>
	)
})
