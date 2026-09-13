# B0X harvest observation playbook

Harvest is an observation tick, not a cycle kickoff. The consumer records it
as `observed_harvest_no_cycle`, with `cycle_created=false` and durable composite
`(lane,event_id)` evidence. Each KST `:13` and `:43` tick has a distinct event
ID. A missed or failed tick remains explicit evidence and is not replayed.

The operation may read supplied, non-secret shadow artifacts only. It cannot
read a broker/account, create a proposal/watch/order, approve an action, or
run a strategy loop.
