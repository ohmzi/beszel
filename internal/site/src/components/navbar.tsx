import { Trans } from "@lingui/react/macro"
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
	HardDriveIcon,
	HeartPulseIcon,
	ListChecksIcon,
	LogOutIcon,
	LogsIcon,
	MenuIcon,
	NetworkIcon,
	PlusIcon,
	RadioIcon,
	SearchIcon,
	ScrollTextIcon,
	ServerIcon,
	SettingsIcon,
	TargetIcon,
	TerminalSquareIcon,
	UserIcon,
	UsersIcon,
	WrenchIcon,
} from "lucide-react"
import { lazy, Suspense, useState } from "react"
import { Button, buttonVariants } from "@/components/ui/button"
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
	{ divider: true },
	{ route: "live", icon: RadioIcon, label: "Live", load: () => import("@/components/routes/live") },
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

function SectionButtons({ className }: { className?: string }) {
	return (
		<div className={cn("flex items-center gap-0.5 overflow-x-auto scrollbar-hide", className)}>
			{SECTIONS.map((s, i) =>
				"divider" in s ? (
					<span key={`d${i}`} className="mx-1 h-6 w-px shrink-0 bg-border" aria-hidden="true" />
				) : (
					<Tooltip key={s.route}>
						<TooltipTrigger asChild>
							<Link
								href={getPagePath($router, s.route)}
								aria-label={s.label}
								className={cn("shrink-0", buttonVariants({ variant: "ghost", size: "icon" }))}
								onMouseEnter={runOnce(s.load)}
							>
								<s.icon className="h-[1.1rem] w-[1.1rem]" strokeWidth={1.5} />
							</Link>
						</TooltipTrigger>
						<TooltipContent>{s.label}</TooltipContent>
					</Tooltip>
				)
			)}
		</div>
	)
}

const isMac = navigator.platform.toUpperCase().indexOf("MAC") >= 0

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
				<Button
					variant="outline"
					className="hidden md:block text-sm text-muted-foreground px-4"
					onClick={() => setCommandPaletteOpen(true)}
				>
					<span className="flex items-center">
						<SearchIcon className="me-1.5 h-4 w-4" />
						<Trans>Search</Trans>
						<span className="flex items-center ms-3.5">
							<Kbd>{isMac ? "⌘" : "Ctrl"}</Kbd>
							<Kbd>K</Kbd>
						</span>
					</span>
				</Button>

				{/* mobile menu */}
				<div className="ms-auto flex items-center text-xl md:hidden">
					<ModeToggle />
					<Button variant="ghost" size="icon" onClick={() => setCommandPaletteOpen(true)}>
						<SearchIcon className="h-[1.2rem] w-[1.2rem]" />
					</Button>
					<DropdownMenu>
						<DropdownMenuTrigger
							onMouseEnter={() => import("@/components/routes/settings/general")}
							className="ms-3"
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
					className="hidden md:flex items-center ms-auto min-w-0"
					onMouseEnter={() => import("@/components/routes/settings/general")}
				>
					<SectionButtons className="min-w-0" />
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
					{!isReadOnlyUser() && (
						<Button variant="outline" className="flex gap-1 ms-2" onClick={() => setAddSystemDialogOpen(true)}>
							<PlusIcon className="h-4 w-4 -ms-1" />
							<Trans>Add System</Trans>
						</Button>
					)}
				</div>
			</div>
			<div className="md:hidden">
				<SectionButtons className="px-1" />
			</div>
		</>
	)
}

const Kbd = ({ children }: { children: React.ReactNode }) => (
	<kbd className="pointer-events-none inline-flex h-5 select-none items-center gap-1 rounded border bg-muted px-1.5 font-mono text-[10px] font-medium text-muted-foreground opacity-100">
		{children}
	</kbd>
)

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
