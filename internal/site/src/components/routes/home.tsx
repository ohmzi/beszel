import { useLingui } from "@lingui/react/macro"
import { useStore } from "@nanostores/react"
import { memo, Suspense, useEffect } from "react"
import SystemsTable from "@/components/systems-table/systems-table"
import SystemDetail from "@/components/routes/system"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { $systems } from "@/lib/stores"

// Ohmz fork: with a single system the home screen IS that system's detail page, so opening the
// dashboard lands straight on the graphs. With more than one system the table stays the home.
export default memo(() => {
	const { t } = useLingui()
	const systems = useStore($systems)
	const solo = systems.length === 1 ? systems[0] : null

	useEffect(() => {
		document.title = solo ? `${solo.name || solo.info?.h || ""} / Beszel` : `${t`All Systems`} / Beszel`
	}, [t, solo])

	if (solo) {
		return <SystemDetail id={solo.id} />
	}

	return (
		<>
			<Suspense>
				<SystemsTable />
			</Suspense>
			<FooterRepoLink />
		</>
	)
})
