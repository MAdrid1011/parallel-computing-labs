# Lab5 核心代码讲解

Lab5 的矩阵乘加由 RTL 电路完成，C++ 程序负责提供矩阵、模拟外存并推进时钟。沿着一个分块的处理过程，可以看到软件怎样驱动电路、数据怎样进入阵列，以及控制器怎样安排下一块的加载。

## 1. C++ 怎样推进一个硬件周期？

`src/simulator.cpp` 中的 `simulate` 创建矩阵、软件 DRAM 和 Verilator 模型。它通过下面的 `step` 推进一次时钟：

{{SOURCE {"path": "Lab5/src/simulator.cpp", "start": "    auto step =", "end": "        dram.update(bus, tick_count); ++tick_count;"}}}

时钟先置低，模型计算当前输出；`dram.drive` 根据请求队列、延迟和停顿条件驱动 ready、valid 与读数据，再次求值后，`dram.sample` 记录本次是否满足传输条件。随后将时钟置高，让 RTL 寄存器在上升沿更新，最后根据采样结果推进软件 DRAM 状态。

一个 `step` 对应一个硬件周期，但波形分别记录上升沿前后两个时间点，所以 VCD 时间每周期增加 2 个 tick。波形显示的时间单位不代表芯片的真实工作频率。

C++ 等待顶层 `done` 后读取输出。用于 `--verify` 的软件矩阵乘法发生在仿真之后，它是参考结果，不参与 RTL 的乘加过程；`host_seconds` 则是电脑运行仿真的时间，与电路的 `cycles` 含义不同。

## 2. DMA 什么时候真正收到数据？

读 DMA 先为 A 的各行发出请求，再读取 B。它的组合逻辑把状态转换成总线信号和 SPM 写入信号：

{{SOURCE {"path": "Lab5/rtl/read_dma.sv", "start": "    assign width =", "end": "    assign spm_data = r_data;"}}}

`matrix_q=0` 时，当前行宽为 K、高为 M，对应 A 分块；为 1 时行宽为 N、高为 K，对应 B 分块。地址状态发出 `ar_valid`，数据状态拉高 `r_ready`。

SPM 写使能来自 `r_valid && r_ready`，因此仅有 valid 或仅有 ready 都不会写入。`ar_beats=(width+3)/4` 把行宽向上取整成数据拍数，因为每拍 64 位可携带 4 个 int16 元素；`spm_lanes` 则说明最后一拍有几个有效元素。

`src/dram.cpp` 根据握手推进地址和拍计数，并对首拍延迟、数据拍间隔与随机停顿建模。这样，改变 DRAM 参数会改变电路实际等了多少周期，而不是简单地在最终结果上加一个延迟常数。

## 3. 一个 PE 怎样完成乘加？

{{SOURCE {"path": "Lab5/rtl/pe.sv"}}}

`product` 是两个有符号 int16 输入的乘积，`sum` 保存 int32 部分和。复位或 clear 时，部分和与传递寄存器清零；enable 时，A、B 和各自的 valid 向相邻 PE 推进。只有两个输入同时有效，才执行 `sum <= sum + product`。

`<=` 是时序逻辑中的非阻塞赋值。在同一个上升沿，各 PE 根据沿到来前的输入计算下一状态，新的输出随后才供相邻 PE 在下一个周期使用。因此，数据每周期向右或向下前进一格，而不是在一个周期内穿过整个阵列。

`rtl/systolic_array.sv` 通过二维 generate 循环连接这些 PE：每行首个 PE 从 `left_a` 取得 A，其余从左邻居取得；每列首个 PE 从 `top_b` 取得 B，其余从上邻居取得。每个 PE 保留一个输出位置的部分和，A 向右复用，B 向下复用。

## 4. SPM 为什么错开各行、各列的输入？

如果所有边缘输入同时送入相同的 k，不同行列的传播距离会使配对错位。`rtl/scratchpad.sv` 根据 lane 编号延迟边缘供数：

{{SOURCE {"path": "Lab5/rtl/scratchpad.sv", "start": "            // Index skew", "end": "                top_b[lane] = b_memory[compute_buffer][lane][(step-32'(lane))*COLUMN_GROUPS+sub_n/ARRAY];"}}}

lane 为 r 的 A 输入从 `step=r` 开始，取 k=`step-r` 的元素；lane 为 c 的 B 输入从 `step=c` 开始，取 k=`step-c` 的元素。随后 A 向右走 c 格，B 向下走 r 格，两者就会在 PE(r,c) 的 `step=k+r+c` 对齐。

例如，PE(1,2) 接收 k=0 的 A 时，A 已在第 1 个 step 进入第 1 行并向右传播两格；对应 B 在第 2 个 step 进入第 2 列再向下传播一格，两者都在 step=3 到达。

边界条件还检查 `sub_m+lane<tile_m`、`sub_n+lane<tile_n`，使尾部不足阵列大小的微块只提供真实数据。越界 lane 的 valid 保持为 0，PE 不会把它计入有效乘加。

## 5. 控制器怎样安排双缓冲？

顶层 `rtl/matrix_accelerator.sv` 用状态机协调读 DMA、SPM、阵列和写 DMA。一个输出块首先清零 C 的 SPM，然后沿 K 维处理多个输入块；每个输入块内部再依次计算阵列大小的输出微块。

当前输入准备好后，控制器进入 `CLEAR_PE`，并在双缓冲模式下预取下一个 K 块：

{{SOURCE {"path": "Lab5/rtl/matrix_accelerator.sv", "start": "                WAIT_INPUT: if", "end": "                RUN_PE: begin"}}}

`current_buffer` 是阵列读取的输入缓冲，`!current_buffer` 是另一份物理缓冲。预取先把另一份的 ready 清零，再向读 DMA 提交下一块的地址。读 DMA 完成后，顶层通过 `load_done` 将对应的 ready 置 1。

`CLEAR_PE` 清空阵列部分和，`RUN_PE` 逐拍推进。运行步数使用 `actual_k + 2*ARRAY - 2`，包含供入 K 个元素以及阵列传播、排空所需的距离。尾部微块也沿用这一固定运行长度，因此有效计算较少时利用率可能降低。

当前 K 块的所有微块都保存后，控制器决定怎样取得下一块：

{{SOURCE {"path": "Lab5/rtl/matrix_accelerator.sv", "start": "                        buffer_ready[current_buffer] <= 0;", "end": "                        end else state <= WRITE_START;"}}}

双缓冲直接切换 `current_buffer`，回到 `WAIT_INPUT` 检查新缓冲是否就绪；单缓冲则此时才发出下一次加载。切换缓冲本身不表示数据已经到齐，只有 ready 为 1 后才能开始下一轮计算。

这也解释了双缓冲的收益来源：它尝试让下一块的传输与当前块的计算重叠。若传输尚未结束，仍然要等；若当前输出块只有一个 K 块，就没有下一 K 块可供预取。

## 6. 为什么清零 PE，却还保留了此前的结果？

PE 只负责当前微块、当前 K 块的部分和。进入 `STORE_C` 后，这份部分和被加到 C 的 SPM，之后即使 PE 清零，已累积的结果仍在片上存储中：

{{SOURCE {"path": "Lab5/rtl/scratchpad.sv", "start": "        if (c_write)", "end": "                            <= c_memory[(c_column+32'(lane))%ARRAY][c_row*COLUMN_GROUPS+(c_column+32'(lane))/ARRAY] + c_values[lane];"}}}

处理一个新输出块时，`c_zero` 让 C 的 SPM 清零；保存微块时，则执行“原有 C + 本次 PE 部分和”。因此，沿 K 维得到的多个乘积块会逐次相加，直到完整输出块完成才启动写 DMA。

C 使用 int32 元素，写 DMA 每个 64 位数据拍最多携带两个输出。最后一拍不足两个元素时，`w_strobe` 标识有效字节，避免覆盖输出范围外的数据。

## 7. 从状态机理解周期统计

{{SOURCE {"path": "Lab5/rtl/matrix_accelerator.sv", "start": "            if (busy) cycles", "end": "            if (write_busy) output_cycles"}}}

`cycles` 在整个任务 busy 时增加；`compute_cycles` 在阵列推进时增加；`input_wait_cycles` 只统计 `WAIT_INPUT` 中当前缓冲尚未就绪的周期。`overlap_cycles` 要求阵列计算与读 DMA busy 同时成立，但 DMA busy 也可能包含等待数据，因此它不等于成功传输的数据拍数。真正接收的数据拍还可以结合 `overlap_read_beats` 查看。

`mac_count` 累加各 PE 在有效输入配对下执行的乘加数。`src/benchmark.cpp` 用它除以 `cycles × ARRAY²` 得到整体 PE 利用率，使用 `compute_cycles` 作分母则得到计算阶段利用率。二者的差距反映了加载、控制和回写等阶段对整体执行的影响。

在[分块、双缓冲与周期](tiling.md)的波形中，可以把这些代码对应到 `state`、`compute_active`、`current_buffer`、`buffer_ready` 和读握手。固定其他参数比较单、双缓冲时，先看是否存在下一 K 块的预取，再看输入等待和总周期是否下降，就能把时间线与统计结果联系起来。
