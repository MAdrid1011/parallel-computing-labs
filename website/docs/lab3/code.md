# Lab3 核心代码讲解

直方图把每个输入值映射到一个桶：输入值 bucket 出现一次，`counts[bucket]` 就增加 1。输入可以分给不同线程读取，输出桶却可能被多个线程同时更新。五种实现的差别，正是怎样协调这些写入。

## 1. 串行计数为什么不能直接加上并行循环？

{{SOURCE {"path": "Lab3/src/histogram_serial.cpp", "start": "void serial(", "end": "    stat.compute_s = lab::now() - begin;"}}}

单线程中，`++counts[bucket]` 正常完成计数。多个线程共享 counts 时，即使它们读取输入的不同位置，也可能读到同一个 bucket。

例如，两个线程都读取到旧计数 5，各自算出 6 并写回，就会丢失一次更新。普通 `++` 在这里没有提供跨线程同步保证；需要下面的保护方式，或改变计数的归属。

## 2. Critical 与 Atomic：保护范围有什么不同？

Critical 实现的核心循环是：

{{SOURCE {"path": "Lab3/src/histogram_critical.cpp", "start": "        #pragma omp for", "end": "            ++local.updates;"}}}

`critical(histogram_update)` 使用同一个具名临界区保护更新。无论 bucket 是否相同，同一时刻只能有一个线程进入这里。`local.updates` 是线程自己的统计数据，放在临界区外即可。

Atomic 把保护缩小到一次指定的内存更新：

{{SOURCE {"path": "Lab3/src/histogram_atomic.cpp", "start": "        #pragma omp for", "end": "            ++local.updates;"}}}

`atomic update` 保证这次 `counts[bucket] += 1` 不会因为并发更新而丢失计数。它没有把其他语句一起保护起来。多个线程仍会争用热点桶及相关缓存行，因此 Atomic 也可能在 `hotspot` 输入下变慢。

输入分布因而会影响两种方式的表现。固定 n、桶数与线程数，在 `uniform` 和 `hotspot` 下比较时，热点输入让更多更新集中到同一个位置，可以帮助分析竞争对运行时间的影响。

## 3. 分段锁：桶号决定拿哪把锁

{{SOURCE {"path": "Lab3/src/histogram_locks.cpp", "start": "        #pragma omp for", "end": "            ++local.updates;"}}}

`bucket % stripes` 把桶映射到一组锁。比如 stripes=16 时，桶 0 与桶 16 共用锁 0，桶 1 使用锁 1。写 counts 前拿锁，写完后释放，所有线程对同一桶必定选择同一把锁。

stripes=1 时，全部更新都争用一把锁；锁数增多后，不同桶有机会同时更新。不过，同一热点桶始终只受一把锁保护，增加锁数不能把桶 0 的更新拆开。锁本身的管理和缓存访问也需要时间。

锁由 `src/workspace.cpp` 在工作区构造时初始化，并在析构时销毁，这些初始化工作不放在每次计数的内循环中。

## 4. Private：把共享写入改成每线程独占

{{SOURCE {"path": "Lab3/src/histogram_private.cpp", "start": "        auto* local_counts =", "end": "        const double merge_begin = lab::now();"}}}

`tid*stride` 为当前线程定位自己的桶数组。所有线程可以同时执行 `++local_counts[input[i]]`，因为它们写的是不同数组，即使 input[i] 相同也不冲突。

`workspace.cpp` 把 stride 向 16 个计数的倍数取整后额外留出 16 个计数，给不同线程的区域留出间隔。这是减少相邻私有区互相影响的布局措施，不应据此假定所有机器都采用某个固定缓存行大小。

这里的屏障不能省略：开始合并之前，需要保证所有线程都已完成自己的计数。`nowait` 先让线程记录 `counted`，随后显式屏障建立这个先后关系。

## 5. 合并时为什么也不需要锁？

{{SOURCE {"path": "Lab3/src/histogram_private.cpp", "start": "        const double merge_begin = lab::now();", "end": "        const double merged = lab::now();"}}}

这段合并循环按输出桶分工：一个 bucket 只交给一个线程，它读取所有线程在这个桶上的局部计数，求和后写入一次全局 counts。此时其他线程已经结束局部写入，输出桶也互不重叠，所以没有共享更新冲突。

假设 2 个线程、4 个桶，局部结果分别为 `[2,1,0,1]` 与 `[1,0,3,0]`，合并后就是 `[3,1,3,1]`。外层 bucket 确定一个输出位置，内层 t 则遍历各线程在该位置上的计数。

Private 用额外存储和合并工作换掉逐元素同步：局部计数空间随“线程数×stride”增长，合并要读取“线程数×桶数”个计数。n 很小时，这些开销可能占据更大比例。

!!! note "怎样读 Private 的时间字段？"
    `merge_s` 单列合并阶段，且已经包含在该线程的 `compute_s` 中，不能再加一次。`wait_s` 记录计数后与合并后的屏障等待；它不测量 Critical 或锁内部的等待，那些时间已包含在内核执行过程中。

## 6. 正确性检查与性能分析

`src/benchmark.cpp` 每轮在计时前把全局 counts 和私有计数清零，计时内包括实际计数与 Private 的合并。计时后再执行 `--verify` 的逐桶对照。

所有桶的和应该等于输入数 n，但这个条件不足以证明每个桶都对。例如 `[4,2]` 与 `[3,3]` 总和相同，分布却不同，因此正确性检查会逐桶比较。

按照[操作步骤](run.md)固定输入，只改变同步方式或锁数，可以将 `elapsed_s` 与实现中的共享更新、屏障等待和合并工作联系起来。是否用了锁只是其中一个因素，线程之间实际争用哪些位置、额外工作占多少，也会影响最终表现。
