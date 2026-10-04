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
				y="24.5"
				textAnchor="middle"
				fontFamily="ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
				fontSize="23"
				fontWeight="700"
				fill="var(--color-primary-foreground)"
			>
				Ω
			</text>
			<path
				d="M8.5 35l4.4-3.2 2.9 2.4 4.5-5.4 3.3 3.5 5.2-6"
				fill="none"
				stroke="var(--color-primary-foreground)"
				strokeWidth="2.2"
				strokeLinecap="round"
				strokeLinejoin="round"
				opacity="0.9"
			/>
		</svg>
	)
}
