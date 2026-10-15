# Lab4 核心代码讲解

Lab4 继续计算 `C = A × B`，但每个 MPI 进程只保存一部分矩阵。A 的本地行决定了本进程负责哪些输出，而这些行的列索引决定了它需要哪些 B 行。围绕这一依赖关系，可以把实现分为数据划分、数据交换和本地计算三个部分。

## 1. 每个进程负责哪些行？

`src/distribution.cpp` 中的 `make_distribution` 计算所有进程的行边界，再取出当前 rank 对应的区间。

{{SOURCE {"path": "Lab4/src/distribution.cpp", "start": "    std::vector<std::size_t> prefix", "end": "    d.b_begin = d.b_offsets[d.rank]; d.b_end = d.b_offsets[d.rank + 1];"}}}

`a_offsets[p]` 是进程 p 所负责的 A 行段的起点，右端是 `a_offsets[p+1]`，同一进程也负责 C 的对应行。`partition=rows` 按行数均分；`partition=nnz` 先构造非零数前缀和，再用 `lower_bound` 寻找接近各份目标工作量的行边界。它不会拆开一行，因此非零数也不保证完全相等。

B 按行数均分，与 A 选择哪种划分方式无关。`b_counts` 计算每个进程持有多少个 B 元素，`b_displacements` 给出这些元素在完整 B 中的起始偏移。两者都以 `MPI_DOUBLE` 元素为单位，不是字节数。

接下来的准备过程使用这些全局行号生成本地矩阵：

{{SOURCE {"path": "Lab4/src/benchmark.cpp", "start": "    const auto d = make_distribution", "end": "    auto workspace = prepare_workspace(cfg, d, a, b, comm);"}}}

本实验能根据全局坐标和种子重复生成相同数据，因此各进程直接生成自己的 A、B 行，没有先由 rank 0 保存整份 A 再分发。生成后的本地 A 用从 0 开始的行号存储，但其列索引仍指向全局 B 行。

## 2. 先获得完整 B，再计算本地 C

Broadcast 的 rank 0 在准备阶段生成完整 B，其他进程为完整 B 分配空间。运行时先广播，再调用本地乘法：

{{SOURCE {"path": "Lab4/src/spmm_full.cpp", "start": "void broadcast(", "end": "    stat.compute_s = MPI_Wtime() - start;"}}}

`MPI_Bcast` 返回后，每个进程都有相同的 `w.full_b`。本地 A 的列索引可以直接索引它，计算结果只写入本地 C。

Allgather 的出发点不同：各进程原本持有不同的 B 行，通过一次集合通信拼出完整 B。

{{SOURCE {"path": "Lab4/src/spmm_full.cpp", "start": "void allgather(", "end": "    stat.compute_s = MPI_Wtime() - start;"}}}

`b_counts` 说明每份数据有多长，`b_displacements` 说明它应该放到完整 B 的哪里。两个版本在通信后使用同一个 `multiply_tiled` 内核，因此比较时主要关注获得 B 的方式及其开销。

## 3. Halo 只收集实际引用的 B 行

如果本地 A 只引用少量不同的列，收集完整 B 就会传来许多用不到的数据。`src/communication_plan.cpp` 先从本地 A 的列索引构造需求集合：

{{SOURCE {"path": "Lab4/src/communication_plan.cpp", "start": "    auto columns = a.col_idx;", "end": "    const auto receive_offsets = displacements(receive_rows);"}}}

排序和去重使一个 B 行只请求一次，即使本地 A 中有很多非零元素引用它。落在 `[b_begin,b_end)` 的行已经在本进程，其他行则根据 `b_offsets` 找到所属进程，并累计需要向它接收的行数。

随后，程序先交换“需要哪些行”的编号，让每个进程知道应该发送自己的哪些行。收到的实际 B 数据保存在 `needed_b` 中：前部放本地所需行，后部放远程所需行。由于行号已经压缩，计划中的 `slot` 会把 A 的全局列号改写为 `needed_b` 中的槽号。

例如，本进程只需要全局 B 的第 1、7、9 行，且第 1 行属于自己，那么 `needed_b` 可以依次保存这三行。相应的 A 列号会从 1、7、9 改成 0、1、2，原有乘法内核因而仍能通过列索引找到正确数据。

准备好计划后，每次 Halo 运行执行：

{{SOURCE {"path": "Lab4/src/spmm_halo.cpp", "start": "    pack_send_rows", "end": "    stat.compute_s = MPI_Wtime() - start;"}}}

`pack_send_rows` 复制当前所需的本地 B 行，并按发送计划打包其他进程请求的行。`MPI_Alltoallv` 允许各进程之间交换不同长度的数据；接收指针跳过本地前缀，将远程行直接写入预留的后部区域。通信完成后，`h.all` 使用改写后的列索引完成乘法。

请求行号的交换与索引改写属于准备阶段，计入 `setup_s`；每轮实际数据的打包、交换和计算计入 `elapsed_s`。因此，`receive_bytes` 描述的是 B 数据的逻辑字节数，没有把计划元数据或 MPI 协议开销算进去。

## 4. Overlap 在等远程行时计算什么？

准备阶段还把 A 的非零元素分成 `h.local` 和 `h.remote`：前者只引用本地 B，后者引用需要接收的远程 B。两部分的行数相同，可以分别计算并累加到同一个 C。

{{SOURCE {"path": "Lab4/src/spmm_overlap.cpp", "start": "    MPI_Request request;", "end": "        stat.compute_s += MPI_Wtime() - start;"}}}

这里的先后关系决定了正确性：

1. `MPI_Ialltoallv` 发起交换后返回一个 request，通信仍可能在进行。
2. `multiply_tiled(h.local, ...)` 只读取 `needed_b` 的本地前缀；通信接收只写远程后缀，两者的访问范围不冲突。
3. `MPI_Wait` 确认请求完成，此后才能读取远程部分。
4. 最后的 `true` 表示把远程贡献加到已有 C，而不是覆盖此前算出的本地贡献。

“累加还是覆盖”在微内核的输出语句中实现：

{{SOURCE {"path": "Lab4/src/spmm_tiled.cpp", "start": "    auto* output = c.values.data()", "end": "        output[lane] = sums[lane] + (add ? output[lane] : 0.0);"}}}

如果省略最后的累加，本地部分就会丢失。另一方面，使用非阻塞接口只创造了重叠的机会：实际收益取决于本地可计算工作量，以及 MPI 实现能否在计算期间推进通信。`wait_s` 记录到达 `MPI_Wait` 后还要等多久，不能仅凭调用了 `MPI_Ialltoallv` 就认定通信已被隐藏。

## 5. 总时间为什么取最慢进程？

{{SOURCE {"path": "Lab4/src/benchmark.cpp", "start": "        stat.rank_elapsed_s =", "end": "        MPI_Reduce(&stat.rank_elapsed_s, &elapsed, 1, MPI_DOUBLE, MPI_MAX, 0, comm);"}}}

每个进程记录自己完成本轮交换与计算的耗时，再用 `MPI_MAX` 汇总。整个结果需要所有进程共同完成，所以一个进程提前结束，并不能让本轮计算提前结束。

按照[操作步骤](run.md)比较时，可以先用 `nnz` 与 `rank_elapsed_s` 看 A 的工作量是否均衡，再用 `local_nnz`、`remote_nnz` 和 `receive_bytes` 看数据依赖。最后比较 Halo 与 Overlap 的 `elapsed_s` 和 `wait_s`，判断本地计算是否提供了足够的重叠空间。汇总的各阶段最大值可能来自不同 rank，不能直接相加还原总时间。
