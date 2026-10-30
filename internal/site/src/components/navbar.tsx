import { Trans } from "@lingui/react/macro"
import { useStore } from "@nanostores/react"
import { getPagePath } from "@nanostores/router"
import {
	ActivityIcon,
	AlertTriangleIcon,
	ArrowRightLeftIcon,
	BellIcon,
	ContainerIcon,
	DatabaseBackupIcon,
	FileTextIcon,
	GaugeIcon,
	GlobeIcon,
	HardDriveIcon,
	HeartPulseIcon,
	ListChecksIcon,
	LogOutIcon,
	LogsIcon,
	MenuIcon,
	NetworkIcon,
	PlusIcon,
	ScrollTextIcon,
	ServerIcon,
	SettingsIcon,
	ShieldAlertIcon,
	TargetIcon,
	TerminalSquareIcon,
	UserIcon,
	UsersIcon,
	WrenchIcon,
} from "lucide-react"
import { lazy, Suspense, useEffect, useRef, useState } from "react"
import { buttonVariants } from "@/components/ui/button"
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuGroup,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuSeparator,
	DropdownMenuSub,
	DropdownMenuSubContent,
	DropdownMenuSubTrigger,
	DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { isAdmin, isReadOnlyUser, logOut, pb } from "@/lib/api"
import { cn, runOnce } from "@/lib/utils"
import { AddSystemDialog } from "./add-system"
import { Logo } from "./logo"
import { ModeToggle } from "./mode-toggle"
import { $router, basePath, Link, navigate, prependBasePath } from "./router"
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip"

const CommandPalette = lazy(() => import("./command-palette"))

// Every section, shown as its own toolbar button (the owner asked for no hidden wrench menu).
// Grouped with a separator: the alert/ops pages first, then the maintenance engine's pages.
const SECTIONS = [
	{ route: "alerts", icon: BellIcon, label: "Alerts", load: () => import("@/components/routes/alerts") },
	{
		route: "incidents",
		icon: AlertTriangleIcon,
		label: "Incidents",
		load: () => import("@/components/routes/incidents"),
	},
	{ route: "reports", icon: FileTextIcon, label: "Reports", load: () => import("@/components/routes/reports") },
	{
		route: "services",
		icon: TerminalSquareIcon,
		label: "Services",
		load: () => import("@/components/routes/services"),
	},
	{ route: "containers", icon: ContainerIcon, label: "All Containers", load: () => null },
	{ route: "smart", icon: HardDriveIcon, label: "S.M.A.R.T.", load: () => null },
	{
		route: "monitors",
		icon: NetworkIcon,
		label: "Network Monitors",
		load: () => import("@/components/routes/monitors"),
	},
	{
		route: "network",
		icon: ShieldAlertIcon,
		label: "Network",
		load: () => import("@/components/routes/network"),
	},
	{
		route: "web",
		icon: GlobeIcon,
		label: "Website",
		load: () => import("@/components/routes/web"),
	},
	{ divider: true },
	{ route: "checks", icon: ListChecksIcon, label: "Checks", load: () => import("@/components/routes/checks") },
	{
		route: "maintenance",
		icon: WrenchIcon,
		label: "Maintenance",
		load: () => import("@/components/routes/maintenance"),
	},
	{ route: "capacity", icon: GaugeIcon, label: "Capacity", load: () => import("@/components/routes/capacity") },
	{ route: "slo", icon: TargetIcon, label: "Service levels", load: () => import("@/components/routes/slo") },
	{ route: "spikes", icon: ActivityIcon, label: "Load spikes", load: () => import("@/components/routes/spikes") },
	{
		route: "migration",
		icon: ArrowRightLeftIcon,
		label: "Migration",
		load: () => import("@/components/routes/migration"),
	},
	{ route: "registry", icon: ScrollTextIcon, label: "Registry", load: () => import("@/components/routes/registry") },
	{
		route: "health",
		icon: HeartPulseIcon,
		label: "Pipeline health",
		load: () => import("@/components/routes/health"),
	},
] as const

// On phones the strip shows each icon with its label, since there is no hover tooltip there.
function SectionButtons({ className, labeled }: { className?: string; labeled?: boolean }) {
	const current = useStore($router)?.route
	const stripRef = useRef<HTMLDivElement>(null)

	// keep the open section in view when the strip scrolls sideways
	useEffect(() => {
		const active = stripRef.current?.querySelector<HTMLElement>('[aria-current="page"]')
		active?.scrollIntoView({ block: "nearest", inline: "center" })
	}, [current])

	return (
		<div ref={stripRef} className={cn("flex items-center gap-0.5 overflow-x-auto scrollbar-hide", className)}>
			{SECTIONS.map((s, i) => {
				if ("divider" in s) {
					return <span key={`d${i}`} className="mx-1 h-6 w-px shrink-0 bg-border" aria-hidden="true" />
				}
				const active = current === s.route
				const link = (
					<Link
						key={s.route}
						href={getPagePath($router, s.route)}
						aria-label={s.label}
						aria-current={active ? "page" : undefined}
						className={cn(
							"shrink-0",
							labeled
								? cn(buttonVariants({ variant: "ghost", size: "sm" }), "h-9 gap-1.5 px-2.5 text-[0.8rem] font-normal")
								: buttonVariants({ variant: "ghost", size: "icon" }),
							active
								? "bg-primary/15 text-primary hover:bg-primary/20 hover:text-primary"
								: "text-muted-foreground hover:text-foreground"
						)}
						onMouseEnter={runOnce(s.load)}
					>
						<s.icon className="h-[1.1rem] w-[1.1rem] shrink-0" strokeWidth={active ? 2 : 1.5} />
						{labeled && <span>{s.label}</span>}
					</Link>
				)
				return labeled ? (
					link
				) : (
					<Tooltip key={s.route}>
						<TooltipTrigger asChild>{link}</TooltipTrigger>
						<TooltipContent>{s.label}</TooltipContent>
					</Tooltip>
				)
			})}
		</div>
	)
}

export default function Navbar() {
	const [addSystemDialogOpen, setAddSystemDialogOpen] = useState(false)
	const [commandPaletteOpen, setCommandPaletteOpen] = useState(false)

	const AdminLinks = AdminDropdownGroup()

	return (
		<>
			<div className="flex items-center h-14 md:h-16 bg-card px-4 pe-3 sm:px-6 border border-border/60 bt-0 rounded-md my-4">
				<Suspense>
					<CommandPalette open={commandPaletteOpen} setOpen={setCommandPaletteOpen} />
				</Suspense>
				<AddSystemDialog open={addSystemDialogOpen} setOpen={setAddSystemDialogOpen} />

				<Link
					href={basePath}
					aria-label="OhmzMaintainer home"
					className="p-2 ps-0 me-3 group flex items-center gap-2"
					onMouseEnter={runOnce(() => import("@/components/routes/home"))}
				>
					<Logo className="h-6 w-6 shrink-0" />
					<span className="hidden sm:inline text-base font-semibold tracking-tight text-foreground">
						Ohmz<span className="font-normal text-muted-foreground">Maintainer</span>
					</span>
				</Link>

				{/* mobile menu */}
				<div className="ms-auto flex items-center text-xl xl:hidden">
					<ModeToggle />
					<DropdownMenu>
						<DropdownMenuTrigger
							onMouseEnter={() => import("@/components/routes/settings/general")}
							className={cn("ms-1", buttonVariants({ variant: "ghost", size: "icon" }))}
							aria-label="Open Menu"
						>
							<MenuIcon />
						</DropdownMenuTrigger>
						<DropdownMenuContent align="end">
							<DropdownMenuLabel className="max-w-40 truncate">{pb.authStore.record?.email}</DropdownMenuLabel>
							<DropdownMenuSeparator />
							<DropdownMenuGroup>
								<DropdownMenuItem
									onClick={() => navigate(getPagePath($router, "settings", { name: "general" }))}
									className="flex items-center"
								>
									<SettingsIcon className="h-4 w-4 me-2.5" />
									<Trans>Settings</Trans>
								</DropdownMenuItem>
								{isAdmin() && (
									<DropdownMenuSub>
										<DropdownMenuSubTrigger>
											<UserIcon className="h-4 w-4 me-2.5" />
											<Trans>Admin</Trans>
										</DropdownMenuSubTrigger>
										<DropdownMenuSubContent>{AdminLinks}</DropdownMenuSubContent>
									</DropdownMenuSub>
								)}
								{!isReadOnlyUser() && (
									<DropdownMenuItem
										className="flex items-center"
										onSelect={() => {
											setAddSystemDialogOpen(true)
										}}
									>
										<PlusIcon className="h-4 w-4 me-2.5" />
										<Trans>Add System</Trans>
									</DropdownMenuItem>
								)}
							</DropdownMenuGroup>
							<DropdownMenuSeparator />
							<DropdownMenuGroup>
								<DropdownMenuItem onSelect={logOut} className="flex items-center">
									<LogOutIcon className="h-4 w-4 me-2.5" />
									<Trans>Log Out</Trans>
								</DropdownMenuItem>
							</DropdownMenuGroup>
						</DropdownMenuContent>
					</DropdownMenu>
				</div>

				{/* desktop nav */}
				{/** biome-ignore lint/a11y/noStaticElementInteractions: ignore */}
				<div
					className="hidden xl:flex items-center ms-auto min-w-0"
					onMouseEnter={() => import("@/components/routes/settings/general")}
				>
					<SectionButtons className="min-w-0" />
					<span className="mx-1.5 h-6 w-px shrink-0 bg-border" aria-hidden="true" />
					<ModeToggle />
					<Tooltip>
						<TooltipTrigger asChild>
							<Link
								href={getPagePath($router, "settings", { name: "general" })}
								aria-label="Settings"
								className={cn(buttonVariants({ variant: "ghost", size: "icon" }))}
							>
								<SettingsIcon className="h-[1.2rem] w-[1.2rem]" />
							</Link>
						</TooltipTrigger>
						<TooltipContent>
							<Trans>Settings</Trans>
						</TooltipContent>
					</Tooltip>
					<DropdownMenu>
						<DropdownMenuTrigger asChild>
							<button aria-label="User Actions" className={cn(buttonVariants({ variant: "ghost", size: "icon" }))}>
								<UserIcon className="h-[1.2rem] w-[1.2rem]" />
							</button>
						</DropdownMenuTrigger>
						<DropdownMenuContent align={isReadOnlyUser() ? "end" : "center"} className="min-w-44">
							<DropdownMenuLabel>{pb.authStore.record?.email}</DropdownMenuLabel>
							<DropdownMenuSeparator />
							{isAdmin() && (
								<>
									{AdminLinks}
									<DropdownMenuSeparator />
								</>
							)}
							<DropdownMenuItem onSelect={logOut}>
								<LogOutIcon className="me-2.5 h-4 w-4" />
								<span>
									<Trans>Log Out</Trans>
								</span>
							</DropdownMenuItem>
						</DropdownMenuContent>
					</DropdownMenu>
				</div>
			</div>
			<nav aria-label="Sections" className="xl:hidden -mt-2 mb-3">
				<SectionButtons
					labeled
					className="-mx-1 px-1 [mask-image:linear-gradient(to_right,black_88%,transparent)] md:flex-wrap md:gap-y-1 md:[mask-image:none]"
				/>
			</nav>
		</>
	)
}

function AdminDropdownGroup() {
	return (
		<DropdownMenuGroup>
			<DropdownMenuItem asChild>
				<a href={prependBasePath("/_/#/collections?collection=users")} target="_blank">
					<UsersIcon className="me-2.5 h-4 w-4" />
					<span>
						<Trans>Users</Trans>
					</span>
				</a>
			</DropdownMenuItem>
			<DropdownMenuItem asChild>
				<a href={prependBasePath("/_/#/collections?collection=systems")} target="_blank">
					<ServerIcon className="me-2.5 h-4 w-4" />
					<span>
						<Trans>Systems</Trans>
					</span>
				</a>
			</DropdownMenuItem>
			<DropdownMenuItem asChild>
				<a href={prependBasePath("/_/#/logs")} target="_blank">
					<LogsIcon className="me-2.5 h-4 w-4" />
					<span>
						<Trans>Logs</Trans>
					</span>
				</a>
			</DropdownMenuItem>
			<DropdownMenuItem asChild>
				<a href={prependBasePath("/_/#/settings/backups")} target="_blank">
					<DatabaseBackupIcon className="me-2.5 h-4 w-4" />
					<span>
						<Trans>Backups</Trans>
					</span>
				</a>
			</DropdownMenuItem>
		</DropdownMenuGroup>
	)
}
