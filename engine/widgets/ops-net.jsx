// ops-net: GET /net (request "state", trigger load) -> data.state.*; footprint 3x2 board tracks (212 px each).
// Lines starting with // are build-time comments and are stripped. Rules: widgets/CONVENTIONS.md.
<Stack gap={7} p={2} style={{opacity:data.state.stale?0.55:1}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={8}>
<Group gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+(data.state.stale?"gray":data.state.hc||"gray")+"-5)"} size={8} withShadow={false} />
<Text fz={11} fw={800} tt="uppercase" lts="0.18em">Network</Text>
</Group>
<Group gap={5} wrap="nowrap">
{data.state.stale&&<Badge size="xs" radius="sm" variant="filled" color="gray">{data.state.ago&&data.state.ago!=="never"?"stale "+data.state.ago:"no data"}</Badge>}
<Badge size="xs" radius="sm" variant="light" color={data.state.stale?"gray":data.state.hc||"gray"} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+(data.state.stale?"gray":data.state.hc||"gray")+"-9),#000 30%),var(--mantine-color-"+(data.state.stale?"gray":data.state.hc||"gray")+"-light-color))"}>{data.state.head||"no data"}</Badge>
</Group>
</Group>
{status.state?.ok===false&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-red-9),#000 30%),var(--mantine-color-red-5))" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}
{data.state.error&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))" lineClamp={2}>{data.state.error}</Text>}
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.12em">Connections, last 24 h</Text>
<SimpleGrid cols={4} spacing={6}>
{(data.state.dirs||[]).map((x,i)=>
<Paper key={i} radius="md" px={8} py={6} bg="light-dark(rgba(0,0,0,0.035),rgba(255,255,255,0.045))" style={{border:"1px solid light-dark(rgba(0,0,0,0.09),rgba(255,255,255,0.08))"}}>
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em" truncate="end">{x.l}</Text>
<Text fz={14} fw={700} lh={1.15} c={data.state.stale?"dimmed":"light-dark(rgba(0,0,0,0.85),rgba(255,255,255,0.92))"} truncate="end">{x.v}</Text>
</Paper>
)}
</SimpleGrid>
<Text fz={11} c="dimmed" lh={1.25} lineClamp={2}>{"Top talkers: "+(data.state.tt||"none")}</Text>
<Text fz={11} c="dimmed" lh={1.25} lineClamp={1}>{"Edge requests 24h "+(data.state.edge||"--")+", blocks "+(data.state.blocked||"--")}</Text>
<Divider />
{(data.state.flags||[]).length===0&&<Text fz={11} c="dimmed" lh={1.25} lineClamp={2}>{data.state.hn||"no open flags"}</Text>}
{(data.state.flags||[]).map((x,i)=>
<Group key={i} gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+x.c+"-5)"} size={8} withShadow={false} />
<Text fz={12} fw={700} truncate="end" style={{flex:1,minWidth:0}}>{x.r}</Text>
<Text fz={11} c="dimmed" truncate="end" maw={110}>{x.t}</Text>
<Badge size="xs" radius="sm" variant="light" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"}>{x.n}</Badge>
</Group>
)}
</Stack>
