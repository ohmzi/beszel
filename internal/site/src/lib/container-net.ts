import { atom } from "nanostores"
import { pb } from "./api"

// Ohmz fork: per-container network usage, keyed "<system>:<container name>". Beszel stores
// container_stats as a list of {n, b:[sent, recv]} (bytes/second) at the same retention tiers as
// system stats: 20m = 24h, 120m = 7d. "Now" is the newest 1m sample.
export type ContainerNet = {
	nowDown: number
	nowUp: number
	d24Down: number
	d24Up: number
	d7Down: number
	d7Up: number
}

export const $containerNet = atom<Record<string, ContainerNet>>({})

type Items = { items: { stats?: { n: string; b?: [number, number] }[] }[] }

async function tier(systemId: string, type: string, perPage = 1000): Promise<Items> {
	return pb
		.send<Items>(
			`/api/collections/container_stats/records?perPage=${perPage}&fields=stats&filter=${encodeURIComponent(`system="${systemId}" && type="${type}"`)}${type === "1m" ? "&sort=-created" : ""}`,
			{ method: "GET" }
		)
		.catch(() => ({ items: [] }) as Items)
}

export async function loadContainerNet(systemId: string): Promise<void> {
	if (!systemId) return
	const [week, day, now] = await Promise.all([tier(systemId, "120m"), tier(systemId, "20m"), tier(systemId, "1m", 1)])
	const out: Record<string, ContainerNet> = {}
	const put = (name: string, k: keyof ContainerNet, v: number) => {
		const key = `${systemId}:${name}`
		const e = (out[key] ??= { nowDown: 0, nowUp: 0, d24Down: 0, d24Up: 0, d7Down: 0, d7Up: 0 })
		e[k] += v
	}
	for (const it of day.items) for (const c of it.stats ?? []) {
		if (Array.isArray(c.b)) {
			put(c.n, "d24Up", (c.b[0] || 0) * 20 * 60)
			put(c.n, "d24Down", (c.b[1] || 0) * 20 * 60)
		}
	}
	for (const it of week.items) for (const c of it.stats ?? []) {
		if (Array.isArray(c.b)) {
			put(c.n, "d7Up", (c.b[0] || 0) * 120 * 60)
			put(c.n, "d7Down", (c.b[1] || 0) * 120 * 60)
		}
	}
	for (const c of now.items?.[0]?.stats ?? []) {
		if (Array.isArray(c.b)) {
			put(c.n, "nowUp", c.b[0] || 0)
			put(c.n, "nowDown", c.b[1] || 0)
		}
	}
	$containerNet.set(out)
}
