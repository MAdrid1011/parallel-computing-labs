# Lab3 核心代码讲解

直方图统计每种数值出现了多少次。例如，输入中出现一次 2，就把第 2 个桶的计数加一。这个计算可以由多个线程共同完成，但不同线程读到相同数值时，会一起修改同一个桶。Lab3 的几个版本围绕这一处共享写入，采用不同的处理方式。

`src/main.cpp` 读取配置后进入 `benchmark`，由 `generate_input` 准备输入，随后准备清零的 counts 和所需工作区。一次运行选择一个计数函数，得到这一份输入的完整直方图：

```text
生成 input → 准备 counts 和工作区 → 选择计数函数 → 得到 counts
                                    ├─ serial
                                    ├─ critical
                                    ├─ atomic
                                    ├─ locks
                                    └─ private_counts（局部计数后合并）
```

## 1. 输入序列与输出计数

设输入有 8 个数，数值都在 0～3 之间：

```text
输入：[0, 1, 0, 2, 0, 3, 1, 0]

桶号：   0  1  2  3
计数： [ 4, 2, 1, 1 ]
```

这里 `n=8` 表示输入长度，`bins=4` 表示有 4 个输出桶。桶号从 0 开始，`counts[2]` 就是数值 2 出现的次数。

串行实现从左到右读取输入，每次把相应的桶加一：

{{SOURCE {"path": "Lab3/src/histogram_serial.cpp", "start": "    for (auto bucket : input)", "end": "    for (auto bucket : input)"}}}

如果使用两个线程，可以各自处理一半输入：

```text
线程 0 读取：[0, 1, 0, 2]
线程 1 读取：[0, 3, 1, 0]
```

两份输入是分开的，但两个线程都可能更新桶 0。普通的加一可以分为读旧值、加一、写回三步。如果桶 0 原先是 0，两个线程都先读到 0，再各自写回 1，就会少算一次。下面的实现都要保证这两个更新能正确保留下来。

## 2. Critical 与 Atomic 都更新同一份输出

Critical 让所有线程共用一个入口，每次修改 counts 都要进入这个临界区：

{{SOURCE {"path": "Lab3/src/histogram_critical.cpp", "start": "            const auto bucket =", "end": "            { ++counts[bucket]; }"}}}

例如，线程 0 更新桶 0 时，线程 1 要等它离开临界区，才能执行自己的更新。即使线程 1 想更新桶 3，也要经过同一个入口。

Atomic 同样保留一份共享 counts，不过直接保证单次加一完整完成：

{{SOURCE {"path": "Lab3/src/histogram_atomic.cpp", "start": "            const auto bucket =", "end": "            counts[bucket] += 1;"}}}

两个线程同时给桶 0 加一时，两次原子更新都会生效，最终从 0 变成 2。这里保护的是紧随 `atomic update` 的那次加法。

Critical 与 Atomic 是两种可选实现。选择 `--kernel critical` 时使用前一种，选择 `--kernel atomic` 时使用后一种。程序不会先完成一遍 Critical，再在它的结果上执行 Atomic。

## 3. Locks 让不同桶使用不同的锁

Locks 也更新共享 counts，但先根据桶号选择锁。假设 `bins=4`、`stripes=2`，就有下面的对应关系：

```text
桶 0 ─┐
桶 2 ─┴─ 使用锁 0

桶 1 ─┐
桶 3 ─┴─ 使用锁 1
```

映射规则是 `bucket % stripes`。更新桶 0 与桶 2 的线程需要同一把锁；更新桶 0 与桶 1 则可以使用不同锁。代码先获取所选的锁，完成加一后再释放：

{{SOURCE {"path": "Lab3/src/histogram_locks.cpp", "start": "            const auto bucket =", "end": "            omp_unset_lock(&lock);"}}}

把 stripes 改成 4，四个桶就分别使用四把锁，输出仍是 `[4,2,1,1]`。把 stripes 改成 1，所有桶都会映射到锁 0。**锁数决定哪些更新需要相互等待，桶数决定输出有多少种计数。**

同一个桶始终映射到同一把锁。因此，输入中如果大部分数值都是 0，即使准备了很多把锁，这些输入仍然集中更新桶 0。

## 4. Private 先保存各线程自己的计数

Private 改变了输出的保存方式：先给每个线程一份完整的 4 桶计数，各自处理自己的输入。

```text
线程 0 的输入：[0, 1, 0, 2]  →  私有计数 [2, 1, 1, 0]
线程 1 的输入：[0, 3, 1, 0]  →  私有计数 [2, 1, 0, 1]
```

两个线程都可以更新自己的桶 0，因为这两个桶存放在不同位置。代码通过线程编号找到自己那一份数组：

{{SOURCE {"path": "Lab3/src/histogram_private.cpp", "start": "        auto* local_counts =", "end": "        auto* local_counts ="}}}

`stride` 表示相邻两份私有数组起点的距离。线程 0 从数组开头使用自己的区域，线程 1 从下一个区域开始。输入循环因此直接更新私有计数：

{{SOURCE {"path": "Lab3/src/histogram_private.cpp", "start": "        for (std::size_t i =", "end": "            ++local_counts[input[i]];"}}}

每个线程拥有所有桶，只读取一部分输入。它们都完成后，程序把相同桶号的计数相加：

```text
桶 0：2 + 2 = 4
桶 1：1 + 1 = 2
桶 2：1 + 0 = 1
桶 3：0 + 1 = 1

得到最终 counts = [4, 2, 1, 1]
```

合并循环按最终输出桶分工，一个桶由一个合并任务负责：

{{SOURCE {"path": "Lab3/src/histogram_private.cpp", "start": "        for (std::size_t bucket =", "end": "            counts[bucket] = total;"}}}

外层 bucket 选定输出桶，内层 t 遍历所有线程在这个桶上的计数。开始合并前的屏障保证各线程已经完成私有写入；合并时，各任务又分别写不同的最终桶。

## 5. 几种配置之间的关系

| 实现 | 计数过程中写到哪里 | 怎样得到最终结果 |
|---|---|---|
| Serial | 一份 counts，由单线程更新 | 逐个输入加一 |
| Critical | 多线程共用一份 counts | 每次加一进入同一个临界区 |
| Atomic | 多线程共用一份 counts | 每次加一使用原子更新 |
| Locks | 多线程共用一份 counts | 根据桶号选锁，再加一 |
| Private | 每线程有一份完整的桶数组 | 各自计数，再逐桶相加 |

`--kernel` 在这些实现之间选择。Critical、Atomic 和 Locks 是共享更新的几种替代方案；Private 则把计算分为“各自计数”和“合并”两步。

对于刚才的例子，各参数控制的内容如下：

| 参数 | 取值示例 | 对计算的影响 |
|---|---|---|
| `n` | 8 | 要读取 8 个输入值 |
| `bins` | 4 | 输出为 4 个桶，允许的输入桶号为 0～3 |
| `threads` | 2 | 由两个线程分担输入；Private 准备两份私有计数 |
| `stripes` | 2 | Locks 使用两把锁，按余数映射；其他内核不使用这些锁 |
| `distribution` | `uniform` 或 `hotspot` | 改变生成的输入值集中在哪些桶 |
| `hot-percent` | 90 | hotspot 生成输入时，大约 90% 的位置定向选择桶 0，其余位置在全部桶中选取 |

hotspot 中，其余随机选取的位置也可能取到 0，因此最终桶 0 的占比可能高于 hot-percent。

改变线程数、锁数或内核时，同一份输入仍应得到同一个直方图。改变 bins 或输入分布，则改变了要统计的数据及其输出含义。脚本中 `Lock1`、`Lock16` 等名称表示同一个 Locks 内核使用不同锁数，并没有在 Critical 或 Atomic 上再叠加一层锁。
