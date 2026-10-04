// ops-jobs: GET /jobs (request "state", trigger load) -> data.state.*; footprint 3x3 board tracks (212 px each).
// Lines starting with // are build-time comments and are stripped. Rules: widgets/CONVENTIONS.md.
<Stack gap={7} p={2} style={{opacity:data.state.stale?0.55:1}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={8}>
<Group gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+(data.state.fail>0?"orange":data.state.stale||!data.state.head?"gray":"teal")+"-5)"} size={8} withShadow={false} />
<Text fz={11} fw={800} tt="uppercase" lts="0.18em">Jobs</Text>
</Group>
<Group gap={5} wrap="nowrap">
{data.state.paused&&<Badge size="xs" radius="sm" variant="filled" color="orange.5" c="dark.9">paused</Badge>}
{data.state.stale&&<Badge size="xs" radius="sm" variant="filled" color="gray">{data.state.ago&&data.state.ago!=="never"?"stale "+data.state.ago:"no data"}</Badge>}
{data.state.fail>0&&<Badge size="xs" radius="sm" variant="filled" color="orange.5" c="dark.9">{data.state.fail+" failing"}</Badge>}
<Badge size="xs" radius="sm" variant="light" color="gray">{data.state.head||"no data"}</Badge>
</Group>
</Group>
{status.state?.ok===false&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-red-9),#000 30%),var(--mantine-color-red-5))" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}
{data.state.error&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))" lineClamp={2}>{data.state.error}</Text>}
{data.state.level&&data.state.level!=="none"&&!data.state.error&&data.state.nt>0&&(data.state.rows||[]).length===0&&<Text fz={12} c="dimmed" ta="center" py={4}>no cleanup jobs have run yet</Text>}
{(data.state.rows||[]).map((x,i)=>
<Group key={i} gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+x.c+"-"+(x.c==="red"?6:5)+")"} size={8} withShadow={false} />
<Stack gap={0} style={{flex:1,minWidth:0}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={6}>
<Text fz={12} fw={700} lh={1.2} truncate="end" style={{flex:1,minWidth:0}}>{x.n}</Text>
<Group gap={4} wrap="nowrap">
{x.f&&<Badge size="xs" radius="sm" variant="light" color="teal" c="light-dark(color-mix(in srgb,var(--mantine-color-teal-9),#000 30%),var(--mantine-color-teal-light-color))">{"freed "+x.f}</Badge>}
<Badge size="xs" radius="sm" variant={x.m==="apply"?"filled":"light"} color={x.m==="apply"?"teal.9":"gray"}>{x.m}</Badge>
</Group>
</Group>
<Text fz={11} c="dimmed" lh={1.25} truncate="end">{x.s}</Text>
</Stack>
<Text fz={10} c={x.x?"light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))":"dimmed"} ta="right" w={34} miw={34}>{x.a}</Text>
</Group>
)}
<Divider />
<Group gap={5} wrap="nowrap">
{(data.state.tiers||[]).map((x,i)=><Badge key={i} size="xs" radius="sm" variant="light" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"}>{x.n+" "+x.a+(x.d?" dry":"")}</Badge>)}
</Group>
</Stack>
