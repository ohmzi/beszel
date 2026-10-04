// ops-load: GET /load (request "state", trigger load) -> data.state.*; footprint 3x3 board tracks (212 px each).
// Lines starting with // are build-time comments and are stripped. Rules: widgets/CONVENTIONS.md.
<Stack gap={5} p={2} style={{opacity:data.state.stale?0.55:1}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={8}>
<Group gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+(data.state.c||"gray")+"-"+(data.state.c==="red"?6:5)+")"} size={8} withShadow={false} />
<Text lh={1.2} fz={11} fw={800} tt="uppercase" lts="0.18em">Load</Text>
<Text lh={1.2} fz={10} c="dimmed" tt="uppercase" lts="0.1em">7 days</Text>
</Group>
<Group gap={5} wrap="nowrap">
{data.state.stale&&data.state.ago&&data.state.ago!=="never"&&<Badge size="xs" radius="sm" variant="filled" color="gray">{"stale "+data.state.ago}</Badge>}
<Badge size="xs" radius="sm" variant="light" color={data.state.stale?"gray":data.state.c||"gray"} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+(data.state.stale?"gray":data.state.c||"gray")+"-9),#000 30%),var(--mantine-color-"+(data.state.stale?"gray":data.state.c||"gray")+"-light-color))"}>{data.state.head||"no data"}</Badge>
</Group>
</Group>
{status.state?.ok===false&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-red-9),#000 30%),var(--mantine-color-red-5))" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}
{data.state.error&&<Text lh={1.2} fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))" lineClamp={2}>{data.state.error}</Text>}
{(data.state.groups||[]).map((g,j)=>
<Stack key={j} gap={4}>
<Group justify="space-between" align="baseline" wrap="nowrap" gap={6}>
<Text lh={1.2} fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em">{g.n}</Text>
<Text lh={1.2} fz={9} c="dimmed" truncate="end">now, avg 1h / 24h / 7d</Text>
</Group>
<SimpleGrid cols={3} spacing={6}>
{(g.tiles||[]).map((x,i)=>
<Paper key={i} radius="md" py={4} px={7} bg="light-dark(rgba(0,0,0,0.035),rgba(255,255,255,0.045))" style={{border:"1px solid light-dark(rgba(0,0,0,0.09),rgba(255,255,255,0.08))"}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={4}>
<Group gap={4} wrap="nowrap" align="center">
<Paper w={9} h={3} radius="xl" bg={x.k} />
<Text lh={1.2} fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.06em" truncate="end">{x.l}</Text>
</Group>
{x.b?<Badge size="xs" radius="sm" variant="light" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"} px={4} h={14}>{x.b}</Badge>:<Text lh={1.2} fz={9} c="dimmed" truncate="end">{x.x}</Text>}
</Group>
<Group gap={3} align="baseline" wrap="nowrap">
<Text fz={20} fw={700} lh={1.15} c={x.c==="teal"||x.c==="gray"?"inherit":"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-5))"}>{x.v}</Text>
<Text lh={1.2} fz={10} c="dimmed">{x.u}</Text>
</Group>
<Text lh={1.2} fz={10} c="dimmed" truncate="end">{x.a}</Text>
</Paper>
)}
</SimpleGrid>
{g.h&&<LineChart h={g.h} data={data.state.rows||[]} dataKey="x" series={g.series||[]} curveType="monotone" withDots={false} withLegend={false} withXAxis={g.xa} gridAxis="x" tickLine="none" strokeWidth={1.75} connectNulls={false} unit={g.uy} lineProps={{isAnimationActive:false}} strokeDasharray="0" xAxisProps={{ticks:data.state.xt,interval:0,tick:{transform:"translate(0, 6)",fontSize:10,fill:"currentColor"}}} yAxisProps={{domain:g.dom,ticks:g.yt,width:g.yw||44,interval:0,tick:{transform:"translate(-4, 0)",fontSize:10,fill:"currentColor"}}} referenceLines={data.state.lx?[{x:data.state.lx,color:"gray.5",strokeDasharray:"3 3"}]:[]} />}
</Stack>
)}
{!(data.state.groups||[]).length&&<Text lh={1.2} fz={12} c="dimmed" ta="center" py={8}>no history yet</Text>}
<Group justify="space-between" align="center" wrap="nowrap" gap={6}>
<Group gap={5} wrap="nowrap" align="center" style={{flex:1,minWidth:0}}>
{data.state.lx&&<Paper w={2} h={10} bg="gray.5" />}
<Text lh={1.2} fz={10} c="dimmed" truncate="end">{data.state.loop||"no ring data"}</Text>
</Group>
<Text lh={1.2} fz={10} c="dimmed" style={{whiteSpace:"nowrap"}}>{"ring "+(data.state.ring||"0/168 h")}</Text>
</Group>
{data.state.full===false&&<Progress value={data.state.rp||0} size={3} radius="xl" color="gray.6" />}
{data.state.extra&&<Text lh={1.2} fz={10} c="dimmed" truncate="end">{data.state.extra}</Text>}
</Stack>
