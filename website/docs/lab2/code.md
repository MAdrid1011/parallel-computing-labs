# Lab2 核心代码讲解

矩阵转置的三个实现都执行 `output[j,i] = input[i,j]`。赋值本身很简单，运行时间却会随循环组织方式发生变化。下面从串行实现的地址变化出发，分析按行并行与二维分块分别改变了什么。

## 1. 转置赋值的两个下标

{{SOURCE {"path": "Lab2/src/transpose_serial.cpp"}}}

`Matrix.values` 是一维数组。对于 n×n 的行优先矩阵，`i*n+j` 表示第 i 行、第 j 列，所以这一句实现 `output[j,i] = input[i,j]`。

固定 i、让 j 依次增加时，输入下标每次加 1，输出下标却每次加 n。例如 n=4、i=1 时，输入访问下标 4、5、6、7，输出写入下标 1、5、9、13。算术工作很少，但跨行写入会影响缓存行的使用。

`stat.updates=n*n` 记录元素搬移次数。`execution.threads=1` 说明此内核只用一个线程，哪怕外部请求了更多线程。

## 2. Naive：把输入行分给线程

{{SOURCE {"path": "Lab2/src/transpose_parallel.cpp", "start": "void parallel_naive(", "end": "        execution.stats[tid] = local;"}}}

`omp for schedule(static)` 把外层 i 循环分给线程，每个线程仍然执行相同的转置赋值。它增加了并行参与者，但没有改变每个线程跨行写入的访问方式。

不同输入行 i 对应不同的输出列，因此不会有两个任务写同一个元素，不需要锁。不过，不同元素可能落在同一条缓存行上，线程之间仍可能产生缓存一致性流量。**没有数据竞争，并不保证内存访问高效。**

`row_visits` 统计线程处理的输入行数，`updates` 统计元素数。`nowait` 后显式记录完成时间，再进入屏障，因而 `wait_s` 表示线程完成任务后的会合等待。

## 3. Tiled：赋值不变，先在小区域内访问

{{SOURCE {"path": "Lab2/src/transpose_tiled.cpp", "start": "        #pragma omp for collapse(2)", "end": "                local.updates += (row_end - bi) * (column_end - bj);"}}}

从外向内看：

1. `bi`、`bj` 指向输入 tile 的左上角，步长都是 `tile`。
2. `collapse(2)` 组合两个块循环，把二维块分给线程。输入的一个矩形块对应输出中转置后的一个矩形块，任务之间写入范围不重叠。
3. `row_end`、`column_end` 用 `min` 处理边缘，因而 n 不必整除 tile。
4. 内部的 i、j 循环保留原来的赋值。输出仍是跨行写入，但访问集中在较小区域，后续 i 会更早回到相邻的输出元素，有机会复用尚在缓存中的缓存行。

例如 n=5、tile=2，最后一行或一列 tile 只有 1 个元素宽。这段代码只访问真实范围，不会通过补零扩大矩阵。

`tile_visits` 是完成的二维块数；`row_visits` 则会对每个列块重复累计其覆盖的行数，因此 Tiled 的这个值可以大于 n。比较正确性与工作总量时，应查看 `updates` 是否为 n²，并使用 `--verify`。

**对应观察：** 固定 n 和线程数，只改变 tile。较大的块可能减少任务数量，却扩大工作区域；较小的块有利于局部访问，也可能增加循环与任务开销。代码没有保证某个 tile 在所有电脑上最快。

## 4. 输出带宽是怎样算出来的？

{{SOURCE {"path": "Lab2/src/benchmark.cpp", "start": "    auto suffix =", "end": "                  << ',' << checksums[trial] << '\\n';"}}}

一个 `uint32_t` 元素占 4 字节。公式用“读一遍输入 + 写一遍输出”的逻辑数据量 `2*n*n*4` 除以耗时，得到 `effective_gbps`。它便于比较不同转置实现，但没有计算缓存行填充、写分配或一致性流量，不能当成硬件实际测到的 DRAM 带宽。

`src/benchmark.cpp` 在计时之后求 checksum，并按需调用 `verify_output`。仅仅交换错位置的两个数，checksum 也可能保持不变；逐元素检查才能确认每个 `output[j,i]` 都与 `input[i,j]` 对应。

## 5. 从访问方式解释测量结果

Serial 与 Naive 使用相同的逐行访问方式，主要区别是输入行由多少线程处理。Naive 与 Tiled 则改变了元素被访问的先后顺序，使缓存复用成为比较的重点。不同 tile 下，`updates` 应保持为 n²，时间变化不能解释为少搬了矩阵元素。

按照[操作步骤](run.md)固定输入与线程数，再比较 `elapsed_s`、`effective_gbps` 和线程记录，可以把任务数量、工作量分配与局部性带来的影响联系起来。
