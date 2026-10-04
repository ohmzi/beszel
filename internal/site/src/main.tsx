import "./index.css"
import { i18n } from "@lingui/core"
import { I18nProvider } from "@lingui/react"
import { useStore } from "@nanostores/react"
import { DirectionProvider } from "@radix-ui/react-direction"
// import { Suspense, lazy, useEffect, StrictMode } from "react"
import { lazy, memo, Suspense, useEffect } from "react"
import ReactDOM from "react-dom/client"
import Navbar from "@/components/navbar.tsx"
import { PipelineStrip } from "@/components/pipeline-strip.tsx"
import { $router } from "@/components/router.tsx"
import Settings from "@/components/routes/settings/layout.tsx"
import { ThemeProvider } from "@/components/theme-provider.tsx"
import { Toaster } from "@/components/ui/toaster.tsx"
import { alertManager } from "@/lib/alerts"
import { isAdmin, pb, updateUserSettings } from "@/lib/api.ts"
import { dynamicActivate, getLocale } from "@/lib/i18n"
import {
	$authenticated,
	$copyContent,
	$direction,
	$newVersion,
	$publicKey,
	$userSettings,
	defaultLayoutWidth,
} from "@/lib/stores.ts"
import * as systemsManager from "@/lib/systemsManager.ts"
import type { BeszelInfo, UpdateInfo } from "./types"

const LoginPage = lazy(() => import("@/components/login/login.tsx"))
const Ack = lazy(() => import("@/components/routes/ack.tsx"))
const Home = lazy(() => import("@/components/routes/home.tsx"))
const Alerts = lazy(() => import("@/components/routes/alerts.tsx"))
const Incidents = lazy(() => import("@/components/routes/incidents.tsx"))
const Reports = lazy(() => import("@/components/routes/reports.tsx"))
const Report = lazy(() => import("@/components/routes/report.tsx"))
const Services = lazy(() => import("@/components/routes/services.tsx"))
const Containers = lazy(() => import("@/components/routes/containers.tsx"))
const Smart = lazy(() => import("@/components/routes/smart.tsx"))
const Monitors = lazy(() => import("@/components/routes/monitors.tsx"))
const Checks = lazy(() => import("@/components/routes/checks.tsx"))
const Registry = lazy(() => import("@/components/routes/registry.tsx"))
const Maintenance = lazy(() => import("@/components/routes/maintenance.tsx"))
const Capacity = lazy(() => import("@/components/routes/capacity.tsx"))
const Slo = lazy(() => import("@/components/routes/slo.tsx"))
const Spikes = lazy(() => import("@/components/routes/spikes.tsx"))
const Migration = lazy(() => import("@/components/routes/migration.tsx"))
const Health = lazy(() => import("@/components/routes/health.tsx"))
const SystemDetail = lazy(() => import("@/components/routes/system.tsx"))
const CopyToClipboardDialog = lazy(() => import("@/components/copy-to-clipboard.tsx"))
const ActiveAlerts = lazy(() => import("@/components/active-alerts.tsx").then((m) => ({ default: m.ActiveAlerts })))

const App = memo(() => {
	const page = useStore($router)

	useEffect(() => {
		// change auth store on auth change
		const unsubscribeAuth = pb.authStore.onChange(() => {
			$authenticated.set(pb.authStore.isValid)
		})
		// get general info for authenticated users, such as public key and version
		pb.send<BeszelInfo>("/api/beszel/info", {}).then((data) => {
			$publicKey.set(data.key)
			// check for updates if enabled
			if (data.cu && isAdmin()) {
				pb.send<UpdateInfo>("/api/beszel/update", {}).then($newVersion.set)
			}
		})
		// get user settings
		updateUserSettings()
		// need to get system list before alerts
		systemsManager.init()
		systemsManager
			// get current systems list
			.refresh()
			// subscribe to new system updates
			.then(systemsManager.subscribe)
			// get current alerts
			.then(alertManager.refresh)
			// subscribe to new alert updates
			.then(alertManager.subscribe)
		return () => {
			unsubscribeAuth()
			alertManager.unsubscribe()
			systemsManager.unsubscribe()
		}
	}, [])

	if (!page) {
		return <h1 className="text-3xl text-center my-14">404</h1>
	} else if (page.route === "home") {
		return <Home />
	} else if (page.route === "alerts") {
		return <Alerts />
	} else if (page.route === "incidents") {
		return <Incidents />
	} else if (page.route === "reports") {
		return <Reports />
	} else if (page.route === "report") {
		return <Report id={page.params.id} />
	} else if (page.route === "services") {
		return <Services />
	} else if (page.route === "system") {
		return <SystemDetail id={page.params.id} />
	} else if (page.route === "containers") {
		return <Containers />
	} else if (page.route === "smart") {
		return <Smart />
	} else if (page.route === "monitors") {
		return <Monitors />
	} else if (page.route === "checks") {
		return <Checks />
	} else if (page.route === "registry") {
		return <Registry />
	} else if (page.route === "maintenance") {
		return <Maintenance />
	} else if (page.route === "capacity") {
		return <Capacity />
	} else if (page.route === "slo") {
		return <Slo />
	} else if (page.route === "spikes") {
		return <Spikes />
	} else if (page.route === "migration") {
		return <Migration />
	} else if (page.route === "health") {
		return <Health />
	} else if (page.route === "settings") {
		return <Settings />
	}
})

const Layout = () => {
	const authenticated = useStore($authenticated)
	const page = useStore($router)
	const copyContent = useStore($copyContent)
	const direction = useStore($direction)
	const { layoutWidth } = useStore($userSettings, { keys: ["layoutWidth"] })

	useEffect(() => {
		document.documentElement.dir = direction
	}, [direction])

	return (
		<DirectionProvider dir={direction}>
			{page?.route === "ack" ? (
				<Suspense>
					<Ack />
				</Suspense>
			) : !authenticated ? (
				<Suspense>
					<LoginPage />
				</Suspense>
			) : (
				<div style={{ "--container": `${layoutWidth ?? defaultLayoutWidth}px` } as React.CSSProperties}>
					<div className="container">
						<Navbar />
					</div>
					<div className="container relative">
						<PipelineStrip />
						<Suspense>
							<ActiveAlerts className="mb-4" />
						</Suspense>
						<App />
						{copyContent && (
							<Suspense>
								<CopyToClipboardDialog content={copyContent} />
							</Suspense>
						)}
					</div>
				</div>
			)}
		</DirectionProvider>
	)
}

const I18nApp = () => {
	useEffect(() => {
		// Activate a locale so I18nProvider can mount App and load the account settings.
		dynamicActivate(getLocale())
	}, [])

	return (
		<I18nProvider i18n={i18n}>
			<ThemeProvider>
				<Layout />
				<Toaster />
			</ThemeProvider>
		</I18nProvider>
	)
}

ReactDOM.createRoot(document.getElementById("app") as HTMLElement).render(
	// strict mode in dev mounts / unmounts components twice
	// and breaks the clipboard dialog
	//<StrictMode>
	<I18nApp />
	//</StrictMode>
)
