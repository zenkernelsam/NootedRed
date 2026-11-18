// 提交守卫：在 -[GFX9AMD_MtlCmdQueue submitCommandBuffers:count:] 入口执行。
// 仅把记录中“已用尾..容量尾”的空闲区填成 PM4 filler (0x80000000)。
// 不解析或改写已用区；若同批其他命令缓冲起点落在空闲区内，则在那里停止。
// 随后执行原方法前 15 字节并跳回。不调用任何外部函数。
// 参数：rdi=self, rsi=_cmd, rdx=buffers, rcx=count。
.text
.globl _submit_guard
_submit_guard:
    pushq %rbx
    pushq %r12
    pushq %r13
    pushq %r14
    pushq %r15
    pushq %rdi
    pushq %rsi
    pushq %rdx
    pushq %rcx
    movq %rdx, %r12             // buffers
    movq %rcx, %r13             // count
    xorl %r14d, %r14d           // i
Lloop:
    cmpq %r13, %r14
    jae Ldone
    movq (%r12,%r14,8), %r10    // buffers[i]
    testq %r10, %r10
    je Lnext
    movq 776(%r10), %r10        // m_members 记录
    testq %r10, %r10
    je Lnext
    movq 8(%r10), %r11          // 起点（CPU）
    movq 16(%r10), %rbx         // 容量尾
    movq 24(%r10), %r15         // 已用尾
    testq %r11, %r11
    je Lnext
    testq $3, %r15              // 记录指针应对齐，异常记录直接跳过
    jne Lnext
    testq $3, %rbx
    jne Lnext
    cmpq %r11, %r15
    jb Lnext
    cmpq %rbx, %r15
    jae Lnext
    movq %rbx, %rax
    subq %r11, %rax             // 块容量
    cmpq $0x100000, %rax        // 超过 1 MiB 的记录视为异常
    ja Lnext
    movq %rbx, %r9              // 填充终点，先取容量尾
    xorl %r8d, %r8d             // j
Loverlap:
    cmpq %r13, %r8
    jae Loverlap_done
    cmpq %r14, %r8
    je Loverlap_next
    movq (%r12,%r8,8), %rax
    testq %rax, %rax
    je Loverlap_next
    movq 776(%rax), %rax
    testq %rax, %rax
    je Loverlap_next
    movq 8(%rax), %rax          // 同批另一命令缓冲的起点
    cmpq %r15, %rax
    jb Loverlap_next            // 在本缓冲已用区之前
    cmpq %r9, %rax
    jae Loverlap_next           // 已越过当前终点
    movq %rax, %r9              // 不覆盖它的命令流
Loverlap_next:
    incq %r8
    jmp Loverlap
Loverlap_done:
    movq %r9, %rax
    subq %r15, %rax             // 可填充字节数
    andq $-4, %rax
    cmpq $4, %rax
    jb Lnext
    xorl %esi, %esi
Lcheck:
    cmpq %rax, %rsi
    jae Lnext                   // 整段已是 filler，无需改写
    cmpl $0x80000000, (%r15,%rsi)
    jne Lfill
    addq $4, %rsi
    jmp Lcheck
Lfill:
    xorl %esi, %esi
Lfill_loop:
    cmpq %rax, %rsi
    jae Lnext
    movl $0x80000000, (%r15,%rsi)
    addq $4, %rsi
    jmp Lfill_loop
Lnext:
    incq %r14
    jmp Lloop
Ldone:
    popq %rcx
    popq %rdx
    popq %rsi
    popq %rdi
    popq %r15
    popq %r14
    popq %r13
    popq %r12
    popq %rbx
    // 原方法前 15 字节：push rbp; mov rbp,rsp; sub rsp,0x10; lea -0x10(rbp),rax; mov rdi,(rax)
    pushq %rbp
    movq %rsp, %rbp
    subq $0x10, %rsp
    leaq -0x10(%rbp), %rax
    movq %rdi, (%rax)
    // 安装时把占位符替换为 方法+15。必须用 r11：原方法前导的
    // lea -0x10(%rbp),%rax 结果会在方法+15 处被下一条指令继续使用。
    movabsq $0x1122334455667788, %r11
    jmpq *%r11
