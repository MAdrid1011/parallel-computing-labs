# Lab1 核心代码讲解

Lab1 的几个实现都计算同一个结果：`C = A × B`。它们的区别在于怎样分配输出、怎样组织循环，以及怎样保存计算中的部分和。从串行内核出发，可以逐步看到这些变化如何影响线程的工作量和数据访问。

## 1. 程序入口与线程调度

`src/main.cpp` 解析参数后调用 `benchmark(cfg)`。Python 的 `run.py` 会多次启动这个程序，改变内核、线程数和输入分布；一次 C++ 调用负责一个配置。

{{SOURCE {"path": "Lab1/src/benchmark.cpp", "start": "    omp_set_dynamic(0);", "end": "    omp_set_schedule(kind, cfg.chunk);"}}}

`omp_set_num_threads` 设置请求的线程数。`omp_set_schedule` 将 `static`、`dynamic` 或 `guided` 写进 OpenMP 的运行时设置，后面的 `schedule(runtime)` 再读取这个设置。因此，Rows 与 Dynamic 使用的是同一个 `parallel_rows` 函数，区别在调度参数。

`static` 预先分配工作；`dynamic` 让线程完成一份后继续领取；`guided` 使用逐渐缩小的工作块。`chunk` 控制调度粒度，与后文的 `block_nnz` 是两件事。

## 2. 一个 CSR 非零元素，更新一段输出列

乘法内核按行遍历 A。外层变量 `i` 确定当前行，内层变量 `p` 遍历这一行在 CSR 数组中的非零元素。由于 CSR 只保存非零项，`p` 是存储位置；对应的矩阵列号 k 要通过 `a.col_idx[p]` 取得。

{{SOURCE {"path": "Lab1/include/detail/row_kernel.hpp", "start": "    for (std::size_t i = begin;", "end": "                output[j] += value * input[j];"}}}

确定非零元素的位置后，程序把它与 B 的对应行相乘：

- `output` 指向 C 的第 i 行；`input` 指向 B 的第 `a.col_idx[p]` 行。乘法使用的是 `A[i,k] × B[k,j]`。
- `row_ptr[i]` 到 `row_ptr[i+1]` 给出 A 第 i 行的非零元素范围，右端不包含在内。程序不会遍历这一行的零元素。
- 每拿到一个 `value`，就沿连续的 j 方向更新 C 的一段列。`omp simd` 提示编译器对这个列循环做向量化；实际效果还取决于编译器与目标机器。

例如，A 第 0 行只有 `A[0,2]=3` 和 `A[0,5]=4`，这段代码会依次把 `3×B[2,:]`、`4×B[5,:]` 加入 `C[0,:]`。调用者已在每次运行前把 C 清零。

## 3. 按行分工：为什么不需要锁？

{{SOURCE {"path": "Lab1/src/spmm_parallel.cpp", "start": "void parallel_rows(", "end": "        execution.stats[tid] = local;"}}}

`omp parallel` 创建线程组，`tid` 标识当前线程。`omp single` 让一个线程记录实际线程数，它末尾的隐式同步保证其他线程随后能读到这个值。

真正分配任务的是 `omp for`：一次循环迭代负责一整行 C。两个线程可以读取相同的 B 行，但不会写同一行 C，所以乘加不需要 `atomic` 或锁。`local` 在并行区域内声明，每个线程有自己的统计对象，最后写到各自的 `execution.stats[tid]`。

`nowait` 去掉循环末尾的隐式屏障，使线程先记下自己的 `finished`；紧接着的显式 `barrier` 再让大家会合。这样可以把“完成本线程任务前的时间”与“完成后等待别人的时间”分开记录。`compute_s` 仍包含调度等开销，不能把它理解为纯乘加指令耗时。

对于 `skewed` 输入，行数相近的任务仍可能包含很不一样的非零数。线程记录中的 `nnz_visits` 与 `wait_s` 可以反映这种差异；再与动态调度的结果比较，就能观察重新分配任务是否减少了等待。

## 4. 按非零数构造行块

{{SOURCE {"path": "Lab1/src/partition.cpp"}}}

`begin` 是尚未组成块的第一行。`row_ptr[i+1] - row_ptr[begin]` 计算从 begin 行到 i 行一共包含多少非零元素；达到 `target_nnz` 就形成一个连续行块，下一块从 i+1 开始。循环后的 `if` 收走不足目标大小的尾块。

这里没有拆开一行，因此 `block_nnz` 是目标值，并不是每块的硬上限：一条特别长的行仍可能让某个块很重。`parallel_balanced` 随后把这些块的编号交给 `omp for`，而不是直接分配行号。分块准备时间另记为 `partition_s`。

这使 `--block-nnz` 成为任务粒度的调节参数。固定调度、线程数与输入时，小块给调度器更多选择，也增加任务数量；大块减少调度次数，但可能留下更重的尾部任务。

## 5. Tiled：一个任务独占 C 的一个矩形

{{SOURCE {"path": "Lab1/src/spmm_tiled.cpp", "start": "        // Each task owns", "end": "                register_block(a, b, c, blocks[block].begin, blocks[block].end, first, last, local);"}}}

`block` 选一段输出行，`panel` 选一段输出列。`collapse(2)` 把两个循环组合成可分配的任务空间，每个任务独占 `C[行块, 列块]`。`min` 截住最右侧不足 tile 的尾块。即使两个线程处理同一组行，只要列块不同，写入位置也不同。

`--tile` 决定列面板的宽度；面板内部还会使用固定宽度 32、8 或更小的微内核：

{{SOURCE {"path": "Lab1/src/spmm_tiled.cpp", "start": "template<std::size_t Width>", "end": "    for (std::size_t lane = 0; lane < Width; ++lane) output[lane] = sums[lane];"}}}

与第 2 节逐次执行 `C += A×B` 不同，这里先把一小段输出的部分和保留在局部 `sums` 中，遍历完这一行的非零元素后才写回 C。设计目的是让编译器使用寄存器与 SIMD，减少对 C 的反复读写；是否全部放入寄存器仍由编译结果决定。

代价是每个微内核都要重新遍历这一行的 CSR 非零元素。因此，`updates` 仍是有效乘加次数，而 `nnz_visits` 可能比 Rows 多，不能只凭它变大就判断程序更慢。

## 6. 计时范围与结果校验

在 `src/benchmark.cpp` 中，`trial=-1` 的那次运行用于预热，不写入测量样本；正式运行选耗时中位数对应的一次。C 的清零发生在计时之前，`verify_output` 发生在记录 `elapsed` 之后，所以 `--verify` 增加的是总运行时间，不会加入内核的 `elapsed_s`。

这些实现将两个优化方向分开呈现：调度和行块改变各线程拿到的工作，列分块与微内核改变数据访问和部分和的保存方式。在[操作步骤](run.md)中固定其他条件，分别改变这两类参数，就能结合线程记录解释耗时为什么变化。
