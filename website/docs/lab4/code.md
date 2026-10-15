# Lab4 核心代码讲解

Lab4 继续计算稀疏矩阵乘法。与 Lab1 的线程共同读取 B 不同，MPI 的每个进程有自己的内存，需要通过消息取得其他进程保存的数据。这里有两个相互联系的决定：每个进程负责计算 C 的哪些行，以及为了计算这些行，需要获得 B 的哪些行。

程序在 `src/main.cpp` 中初始化 MPI，各进程进入 `benchmark` 后沿着同一条流程执行：

```text
make_distribution 确定归属
    → 生成本进程的 A 行和 B 行
    → prepare_workspace 准备本地行块与所需数据空间
    → 所选 kernel 取得 B 并完成乘法
    → 得到本进程负责的 C 行
```

C 的全部行由各进程的这些本地结果共同组成，计算完成后仍按行分布保存在各进程中。下面先确定数据归属，再看不同 kernel 怎样完成这条流程。

## 1. 把同一个乘法分给两个进程

沿用 Lab1 的小矩阵：

```text
A = [ 2  0  0 ]       B = [ 1   2   3   4 ]
    [ 0  3  0 ]           [ 5   6   7   8 ]
    [ 4  0  5 ]           [ 9  10  11  12 ]
```

现在使用两个进程，编号分别是 rank 0 和 rank 1。A 三行的非零数为 1、1、2，按非零数划分时，可以把前两行交给 rank 0，最后一行交给 rank 1：

| 进程 | 保存的 A 行 | 负责的 C 行 | 本来持有的 B 行 |
|---|---|---|---|
| rank 0 | 第 0、1 行 | 第 0、1 行 | 第 0 行 |
| rank 1 | 第 2 行 | 第 2 行 | 第 1、2 行 |

B 按行数划分。由于本例 B 有 3 行，代码使用的边界公式得到 rank 0 持有第 0 行、rank 1 持有第 1、2 行。

负责某个 C 行，并不意味着所需 B 数据也在这个进程。例如 rank 0 要计算 `C[1,:] = 3 × B[1,:]`，但 B 的第 1 行在 rank 1。进程之间的通信就用来补齐这种计算依赖。

## 2. partition 只决定 A 和 C 怎样分行

设置 `partition=rows` 时，程序根据行数划分 A。对这个 3 行的例子，rank 0 得到第 0 行，rank 1 得到第 1、2 行。

设置 `partition=nnz` 时，根据累计非零数寻找行边界。本例共有 4 个非零元素，两个进程各承担约 2 个，因此 rank 0 得到前两行，rank 1 得到最后一行。两种划分都保留完整的行。

{{SOURCE {"path": "Lab4/src/distribution.cpp", "start": "    for (int p = 0; p <= d.ranks;", "end": "        d.b_offsets[p] = cfg.k * p / d.ranks;"}}}

`a_offsets` 保存 A 的分界位置，使用 rows 或 nnz 对应的规则；`b_offsets` 则始终根据 B 的行数划分。`prefix` 是 A 的非零数前缀和，用来找到累计工作量接近各份目标的位置。

下面继续使用 `partition=nnz` 的分配。Broadcast、Allgather、Halo 和 Overlap 都可以在这个分配上运行，它们改变的是如何取得 B，而不是重新决定由谁计算 C 的某一行。

## 3. Broadcast 与 Allgather 都让各进程获得完整 B

Broadcast 由 rank 0 准备完整 B，再把它广播给所有进程：

{{SOURCE {"path": "Lab4/src/spmm_full.cpp", "start": "    MPI_Bcast(", "end": "    MPI_Bcast("}}}

本例广播后，rank 0 与 rank 1 都有 B 的全部 3 行。rank 0 用它们计算 C 的前两行，rank 1 用它们计算最后一行。

Allgather 则让每个进程贡献原本持有的 B 行，一起拼出完整 B：

```text
rank 0 提供 B 第 0 行 ──┐
                       ├─ 每个进程得到 B 的第 0、1、2 行
rank 1 提供 B 第 1、2 行 ┘
```

{{SOURCE {"path": "Lab4/src/spmm_full.cpp", "start": "    MPI_Allgatherv(", "end": "                   w.full_b.values.data(), d.b_counts.data(), d.b_displacements.data(), MPI_DOUBLE, comm);"}}}

`b_counts` 说明各进程提供多少个元素，`b_displacements` 说明各份数据放在完整 B 的哪里。获得完整 B 后，两种实现都调用同一个 `multiply_tiled` 完成本地乘法。

它们是两种获得完整 B 的方式：Broadcast 从 rank 0 发出整份数据，Allgather 从各进程收集各自的数据片段。

## 4. Halo 根据 A 的非零列号，只交换需要的 B 行

本例中，rank 0 的 A 行只引用 B 的第 0、1 行，rank 1 的 A 行只引用 B 的第 0、2 行。因此可以只补齐缺少的部分：

| 进程 | 计算所需的 B 行 | 已经在自己这里 | 需要向其他进程取得 |
|---|---|---|---|
| rank 0 | 0、1 | 0 | 向 rank 1 取得第 1 行 |
| rank 1 | 0、2 | 2 | 向 rank 0 取得第 0 行 |

Halo 就使用这个规则。A 的列号 k 告诉程序需要 B 的第 k 行；同一行 B 被引用多次时，只需要请求一份。

{{SOURCE {"path": "Lab4/src/communication_plan.cpp", "start": "    auto columns = a.col_idx;", "end": "    columns.erase(std::unique(columns.begin(), columns.end()), columns.end());"}}}

排序和去重后，程序区分已有的行和需要请求的行，并交换请求列表。正式搬运数据时，用 `MPI_Alltoallv` 按计划发送所需 B 行：

{{SOURCE {"path": "Lab4/src/spmm_halo.cpp", "start": "    MPI_Alltoallv(", "end": "                  h.receive_counts.data(), h.receive_displacements.data(), MPI_DOUBLE, comm);"}}}

收集到的行保存在紧凑矩阵 `needed_b` 中。本例 rank 1 已有 B 第 2 行，将它放在槽 0，收到的 B 第 0 行放在槽 1。A 原来的列号 0、2 也相应改成槽号 1、0，原有乘法循环就能在这份较小的矩阵中找到正确数据。

Halo 先等所需的行到齐，再完成本地乘法。与收集完整 B 相比，它改变了保存和交换的数据范围，结果 C 保持相同。

## 5. Overlap 先计算已经具备数据的部分

Overlap 使用与 Halo 相同的需求列表，但把计算分成已有数据和远程数据两部分。对 rank 1 的输出行，有：

```text
C 的第 2 行 = 4 × B 的第 0 行 + 5 × B 的第 2 行
                需要接收             本地已有
```

因此 rank 1 可以发起交换后，先用已有的 B 第 2 行计算一份部分结果：

```text
先计算本地贡献：5 × [9,10,11,12] = [45,50,55,60]
收到远程行以后：4 × [1, 2, 3, 4] = [ 4, 8,12,16]
两份贡献相加：                   [49,58,67,76]
```

源码先用非阻塞接口发起交换：

{{SOURCE {"path": "Lab4/src/spmm_overlap.cpp", "start": "    MPI_Ialltoallv(", "end": "                   h.receive_counts.data(), h.receive_displacements.data(), MPI_DOUBLE, comm, &request);"}}}

这个调用返回后，数据可能还在传输，程序可以继续执行其他计算。`h.local` 保存只引用本地 B 行的 A 元素，用来先计算本地贡献：

{{SOURCE {"path": "Lab4/src/spmm_overlap.cpp", "start": "    multiply_tiled(h.local,", "end": "    multiply_tiled(h.local,"}}}

这里的 local 和 remote 根据 **B 的行位于哪个进程**来区分。同一行 A 可以同时含有两种元素，就像本例 A 第 2 行中的 4 和 5。

读取远程数据前，`MPI_Wait` 确认接收完成，随后把 `h.remote` 的贡献加到已有结果：

{{SOURCE {"path": "Lab4/src/spmm_overlap.cpp", "start": "    MPI_Wait(&request,", "end": "    MPI_Wait(&request,"}}}

{{SOURCE {"path": "Lab4/src/spmm_overlap.cpp", "start": "        multiply_tiled(h.remote,", "end": "        multiply_tiled(h.remote,"}}}

最后一个参数 `true` 表示保留原有 C，再加本次部分和。输出语句为：

{{SOURCE {"path": "Lab4/src/spmm_tiled.cpp", "start": "        output[lane] = sums[lane]", "end": "        output[lane] = sums[lane]"}}}

这一步把 `[45,50,55,60]` 与 `[4,8,12,16]` 合在一起。Halo 与 Overlap 请求相同的远程行；Overlap 进一步安排了这些行到达前可以完成的计算。

## 6. 进程划分与本地分块的组合

| 配置项 | 决定什么 | 在本例中的含义 |
|---|---|---|
| `ranks` | 参加计算的 MPI 进程数 | 使用 rank 0、rank 1 两个进程 |
| `partition` | 每个进程负责哪些 A 行和 C 行 | nnz 划分使前两行和最后一行分属两个进程 |
| `kernel` | 怎样获得 B、怎样安排本地与远程贡献 | 选择 Broadcast、Allgather、Halo 或 Overlap |
| `block-nnz` | 一个进程内部怎样把自己的 A 行组成行块 | 分组本地行，不会再创建 MPI 进程 |
| `tile` | 本地乘法一次处理多少输出列 | tile=2 时，4 列分为前两列、后两列 |

可以把整个过程看成两层：先决定各进程拥有哪些输出行，再在每个进程内部按行块和列块组织乘法。这里的本地内核按循环依次处理这些块，Lab4 没有再为这些块启动 OpenMP 线程。

改变 partition 会改变进程的工作和所需 B 行；在相同 partition 上改变通信内核，则保留输出行的归属，改变取得数据及计算部分和的过程。这两类配置可以组合使用。
