import { useEffect, useState } from "react"
import { pb } from "./api"

// Ohmz fork: fetch one of the maintenance engine's published JSON files (proxied by the hub).
// Read-only; the hub allowlists the file names.
export function useMaintenanceFile<T = unknown>(name: string): T | null {
	const [data, setData] = useState<T | null>(null)
	useEffect(() => {
		let live = true
		const load = () =>
			pb
				.send<T>(`/api/beszel/maintenance/file?name=${encodeURIComponent(name)}`, { method: "GET" })
				.then((d) => live && setData(d))
				.catch(() => {})
		load()
		const t = setInterval(load, 30000)
		return () => {
			live = false
			clearInterval(t)
		}
	}, [name])
	return data
}

// Ohmz fork: fetch one published maintenance report (daily / weekly) by id.
export function useMaintenanceReport<T = unknown>(id: string): T | null {
	const [data, setData] = useState<T | null>(null)
	useEffect(() => {
		let live = true
		pb.send<T>(`/api/beszel/maintenance/report?id=${encodeURIComponent(id)}`, { method: "GET" })
			.then((d) => live && setData(d))
			.catch(() => {})
		return () => {
			live = false
		}
	}, [id])
	return data
}

export const DOT: Record<string, string> = {
	ok: "bg-green-500",
	info: "bg-blue-500",
	warn: "bg-yellow-500",
	crit: "bg-red-500",
	error: "bg-red-600",
	skipped: "bg-muted-foreground/50",
	open: "bg-red-500",
	acknowledged: "bg-muted-foreground",
	pending: "bg-yellow-500",
	observe: "bg-blue-500",
	retired: "bg-green-500",
	"dry-run": "bg-blue-500",
	done: "bg-green-500",
	failed: "bg-red-500",
	refused: "bg-muted-foreground",
	closed: "bg-muted-foreground",
}

export const dotFor = (s?: string) => DOT[s ?? ""] ?? "bg-muted-foreground/50"

export function when(ts?: number | null): string {
	if (!ts) return ""
	const diff = Math.round(Date.now() / 1000 - ts)
	const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
	if (Math.abs(diff) < 3600) return rtf.format(-Math.round(diff / 60), "minute")
	if (Math.abs(diff) < 86400) return rtf.format(-Math.round(diff / 3600), "hour")
	return rtf.format(-Math.round(diff / 86400), "day")
}

export const bytes = (n?: number) => {
	if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) return "-"
	const u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
	let i = 0
	while (n >= 1024 && i < u.length - 1) {
		n /= 1024
		i++
	}
	return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${u[i]}`
}

export const dur = (s?: number) => {
	if (typeof s !== "number" || !Number.isFinite(s) || s < 0) return "-"
	if (s < 90) return `${Math.round(s)}s`
	if (s < 5400) return `${Math.round(s / 60)} min`
	if (s < 172800) return `${Math.round(s / 3600)} h`
	return `${Math.round(s / 86400)} d`
}
