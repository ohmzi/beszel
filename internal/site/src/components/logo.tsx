// OhmzMaintainer mark (Ohmz fork): an amber tile with the Ω glyph over a rising usage line.
export function Logo({ className }: { className?: string }) {
	return (
		<svg
			xmlns="http://www.w3.org/2000/svg"
			viewBox="0 0 40 40"
			className={className}
			role="img"
			aria-label="OhmzMaintainer"
		>
			<rect width="40" height="40" rx="11" fill="var(--color-primary)" />
			<text
				x="20"
				y="25"
				textAnchor="middle"
				fontFamily="ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
				fontSize="22"
				fontWeight="700"
				fill="var(--color-primary-foreground)"
			>
				Ω
			</text>
			<path
				d="M9.5 35l4.6-3.3 3 2.5 4.6-5.6 3.4 3.6 5.3-6.1"
				fill="none"
				stroke="var(--color-primary-foreground)"
				strokeWidth="2.2"
				strokeLinecap="round"
				strokeLinejoin="round"
				opacity="0.85"
			/>
		</svg>
	)
}
