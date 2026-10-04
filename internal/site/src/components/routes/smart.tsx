import { useEffect } from "react"
import SmartTable from "@/components/routes/system/smart-table"
import { FooterRepoLink } from "@/components/footer-repo-link"

export default function Smart() {
	useEffect(() => {
		document.title = "OhmzMaintainer"
	}, [])

	return (
		<>
			<SmartTable />
			<FooterRepoLink />
		</>
	)
}
