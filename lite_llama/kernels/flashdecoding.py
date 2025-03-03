import triton, torch
import triton.language as tl
from torch.cuda.amp import custom_fwd

@triton.jit
def detect_nan_kernel(input_ptr, output_ptr, N, BLOCK_SIZE: tl.constexpr):
    pid = tl.program_id(0)
    offsets = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    mask = offsets < N
    x = tl.load(input_ptr + offsets, mask=mask, other=0.0)
    is_nan = x!=x # NaN != NaN
    tl.store(output_ptr + offsets, is_nan, mask=mask)

def detect_nan(input_tensor):
    N = input_tensor.numel()
    BLOCK_SIZE = 1024
    grid = (triton.cdiv(N, BLOCK_SIZE),)
    output = torch.zeros_like(input_tensor, dtype=torch.int32)
    detect_nan_kernel[grid](input_tensor, output, N, BLOCK_SIZE)
    return output

@triton.jit
def _flash_decoding_stage1_kernel(
    Q, K, V, qk_scale,
	B_Start_Loc, B_Seqlen, 
	num_kv_groups, # group of kv heads
    Mid_O, Mid_O_LogExpSum,

    q_bs_stride, q_heads_stride, q_dim_stride,  # Q 的 strides
    k_bs_stride, k_heads_stride, k_dim_stride,  # K 的 strides
    v_bs_stride, v_heads_stride, v_dim_stride,  # V 的 strides

    mido_batch_stride, mido_heads_stride, mido_partitions_stride, mido_dim_stride,
    mido_les_batch_stride, mido_les_heads_stride, mido_les_partitions_stride,

    BLOCK_SEQ: tl.constexpr, # 默认 128
    BLOCK_N: tl.constexpr,   # 默认 32
    BLOCK_DMODEL: tl.constexpr,
):
	"""Flash Attention Stage1 Triton Kernel"""
	# 获取当前程序的 block 在各个维度上的索引
	batch_pid = tl.program_id(0)
	head_pid = tl.program_id(1)
	seq_block_pid = tl.program_id(2)
	kv_head_pid = head_pid // num_kv_groups

	# 计算当前批次的起始位置
	cur_batch_seq_len = tl.load(B_Seqlen + batch_pid)
	cur_batch_start_loc = tl.load(B_Start_Loc + batch_pid)
	# cur_batch_start_loc = batch_pid * actual_seq_len
	
	# 计算当前分区的起始和结束索引
	cur_batch_partition_start_index = seq_block_pid * BLOCK_SEQ
	cur_batch_partition_end_index = tl.minimum(cur_batch_seq_len, cur_batch_partition_start_index + BLOCK_SEQ)

	# 计算需要处理的块数
	num_blocks = tl.where(cur_batch_partition_end_index - cur_batch_partition_start_index <= 0, 
                       	0, (cur_batch_partition_end_index - cur_batch_partition_start_index + BLOCK_N - 1) // BLOCK_N)

	# 初始化偏移向量
	offs_n = cur_batch_partition_start_index + tl.arange(0, BLOCK_N)  # [BLOCK_N]
	offs_d = tl.arange(0, BLOCK_DMODEL)  # [BLOCK_DMODEL]
    
	# 计算 Q 的偏移量
	q_offs = (
		batch_pid * q_bs_stride
		+ head_pid * q_heads_stride
		+ offs_d * q_dim_stride
	)

	# 计算 K 和 V 的偏移量
	k_offs = (
		(cur_batch_start_loc + offs_n[:, None]) * k_bs_stride
		+ kv_head_pid * k_heads_stride
		+ offs_d[None, :] * k_dim_stride
	)

	v_offs = (
		(cur_batch_start_loc + offs_n[:, None]) * v_bs_stride
		+ kv_head_pid * v_heads_stride
		+ offs_d[None, :] * v_dim_stride
	)
    
	# 获取指针
	q_ptrs = Q + q_offs
	k_ptrs = K + k_offs
	v_ptrs = V + v_offs

	# 加载 Q 向量
	q = tl.load(q_ptrs)  # [BLOCK_DMODEL]

	# 初始化归一化项和累加器
	d_i = 0.0  # 标量 # 使用小的正数而不是0
	m_i = -float("inf")  # 标量
	acc = tl.zeros([BLOCK_DMODEL], dtype=tl.float32)  # [BLOCK_DMODEL]

	# 迭代处理每个块
	for start_n in range(0, num_blocks, 1):
		offs_n_new = start_n * BLOCK_N + offs_n  # [BLOCK_N]
		# 生成 K 的掩码
		k_mask = offs_n_new < cur_batch_partition_end_index  # [BLOCK_N]

		# 加载 K 和 V
		k = tl.load(k_ptrs, mask=k_mask[:, None], other=0.0)  # [BLOCK_N, BLOCK_DMODEL]
		v = tl.load(v_ptrs, mask=k_mask[:, None], other=0.0)  # [BLOCK_N, BLOCK_DMODEL]
		# if head_pid == 3:
		# 	tl.device_print(f"k", k)
		# 	tl.device_print(f"v", v)

		# 计算 qk^T
		# qk = tl.zeros((BLOCK_M_SIZE, BLOCK_N_SIZE), dtype=tl.float32)
		qk = tl.sum(q[None, :] * k, axis=1)  # [BLOCK_N]
		qk *= qk_scale
		qk = tl.where(k_mask, qk, float("-inf"))  # [BLOCK_N]

		# 更新最大值项和 qk 项
		current_max = tl.max(qk)  # 标量
		m_ij = tl.maximum(m_i, current_max)  # 标量
		p = tl.exp(qk - m_ij)  # [BLOCK_N]
		
		# 更新归一化项
		alpha = tl.exp(m_i - m_ij) 
		d_i = alpha * d_i + tl.sum(p, axis=0)

		# 更新 attention 输出累加器
		acc = alpha * acc + tl.sum(p[:, None] * v, axis=0)  # [BLOCK_DMODEL]
		# acc = acc * alpha + tl.dot(p, v)  # [BLOCK_DMODEL]
		
		# 更新归一化器
		m_i = m_ij
		# 更新 K 和 V 的指针
		k_ptrs += BLOCK_N * k_bs_stride
		v_ptrs += BLOCK_N * v_bs_stride

        
	# 计算是否需要存储
	need_store = num_blocks > 0  # 标量布尔值

	# 计算存储的偏移量
	off_mid_o = (
		batch_pid * mido_batch_stride
		+ head_pid * mido_heads_stride
		+ seq_block_pid * mido_partitions_stride
		+ offs_d * mido_dim_stride
	)

	off_mid_o_les = (
		batch_pid * mido_les_batch_stride
		+ head_pid * mido_les_heads_stride
		+ seq_block_pid * mido_les_partitions_stride
	)

	# 计算最终的 attention 输出和 log-sum-exp
    # 为了防止 d_i 为零，添加一个小的常数
	part_atten_out = acc / (d_i) # [BLOCK_DMODEL]
	logexpsum = m_i + tl.log(d_i) # 标量

	# 条件存储
	part_atten_out = tl.where(need_store, part_atten_out, 0.0)  # [BLOCK_DMODEL]
	logexpsum = tl.where(need_store, logexpsum, float("-inf"))  # 标量

	# 存储结果
	tl.store(Mid_O + off_mid_o, part_atten_out, mask=need_store)
	tl.store(Mid_O_LogExpSum + off_mid_o_les, logexpsum, mask=need_store)

	# need_store = tl.where(num_blocks == 0, 0, 1)
	# for _ in range(0, need_store, 1):
	# 	tl.store(Mid_O + off_mid_o, acc / d_i)
	# 	tl.store(Mid_O_LogExpSum + off_mid_o_les, m_i + tl.log(d_i))

@torch.no_grad()
def flash_decode_stage1(
    q, k, v,         # Q: [batchs, num_heads, head_dim], K, V: [batchs * seq_len, num_heads, head_dim]
    qk_scale, 
	b_start_loc, b_seq_len, 
	max_actual_seq_len,  # 最大的实际序列长度
    mid_o, mid_o_logexpsum, # Mid_O: [batchs, num_heads, cdiv(seq_len, PARTITION_SIZE), head_dim], Mid_O_LogExpSum: [batchs, num_heads, cdiv(seq_len, PARTITION_SIZE)]
    PARTITION_SIZE,
):
	BLOCK_N_SIZE = 16

	# BLOCK_DMODEL = q.shape[-1]
	assert PARTITION_SIZE % BLOCK_N_SIZE == 0, "PARTITION_SIZE 必须是 BLOCK_N_SIZE 的倍数"

	batchs, num_heads, head_dim = q.shape # decode 阶段 q 张量的 seq_len = 1, 这里的 batchs 实际就是 batch_size
	
	# grid 配置的并行度比 flashattention1-2 多了 kv cache seq 维度
	grid = (batchs, num_heads, triton.cdiv(max_actual_seq_len + PARTITION_SIZE - 1, PARTITION_SIZE))
	num_kv_groups = q.shape[1] // k.shape[1] # num_q_heads // num_k_heads

	_flash_decoding_stage1_kernel[grid](
		q, k, v, qk_scale,
        b_start_loc, b_seq_len, 
		num_kv_groups,   # kv 组数量
		mid_o, mid_o_logexpsum,
		*q.stride(),
		*k.stride(),
		*v.stride(),
		*mid_o.stride(),
		*mid_o_logexpsum.stride(),

		BLOCK_SEQ = PARTITION_SIZE,
		BLOCK_N = BLOCK_N_SIZE,
		BLOCK_DMODEL = head_dim,
		num_warps = 2,
		num_stages = 2,
	)

@triton.jit
def _flash_decoding_stage2_kernel(
	Mid_O,  		# [batch, head, seq_block_num, head_dim]
	Mid_O_LogExpSum,  # [batch, head, seq_block_num]
	Ouput,          # attention 输出首地址
	mido_batch_stride, mido_heads_stride, mido_partitions_stride, mido_dim_stride,
	mido_les_batch_stride, mido_les_heads_stride, mido_les_partitions_stride,
	o_bs_stride, o_heads_stride, o_dim_stride,
	B_Seqlen,   # TODO 支持 PagedAttention 和连续批处理
	BLOCK_DMODEL: tl.constexpr,
	BLOCK_SEQ: tl.constexpr, # type: ignore
):
    """Reduction (online softmax)
    """
    batch_pid = tl.program_id(0)
    head_pid = tl.program_id(1)
    cur_batch_seq_len = tl.load(B_Seqlen + batch_pid)
    
    # 初始化偏移 
    offs_d = tl.arange(0, BLOCK_DMODEL)

	# 最后一个维度 stride 为 1 可省略, 如 mido_dim_stride
    offs_part_v = batch_pid * mido_batch_stride \
                + head_pid * mido_heads_stride \
                + offs_d

    offs_part_max = batch_pid * mido_les_batch_stride \
                + head_pid * mido_les_heads_stride

    part_v_ptrs = Mid_O + offs_part_v
    part_max_ptrs = Mid_O_LogExpSum + offs_part_max

    # Reduce kv 分块相关变量值. num_partitions 是 kv 分块数量
    d_i = 0.0
    m_i = -float("inf")
    acc = tl.zeros([BLOCK_DMODEL], dtype=tl.float32)
    
    num_partitions = (cur_batch_seq_len + BLOCK_SEQ - 1) // BLOCK_SEQ
    
    for block_seq_n in range(0, num_partitions, 1): # TODO 有 bug 需要修复
        part_v = tl.load(part_v_ptrs)
        part_max = tl.load(part_max_ptrs)

        # -- 更新局部最大值 -- #
        m_ij = tl.maximum(part_max, m_i)

        # -- 计算 alpha = exp(m{j-1} - m{j}) 值 -- #
        alpha = tl.exp(m_i - m_ij)

        # -- 更新归一化项和 attention 输出累加器 -- #
        p = tl.exp(part_max - m_ij)
        acc = alpha * acc + p * part_v

        # alpha * d_i: 缩放 d_i, p * weight: 当前元素的指数值 * 权重
        d_i = alpha * d_i + p

        # 更新 max 值和指针偏移
        m_i = m_ij
        part_v_ptrs += mido_partitions_stride
        part_max_ptrs += mido_les_partitions_stride

    # -- 更新 attention 输出累加器 -- #
    offs_out = batch_pid * o_bs_stride + head_pid * o_heads_stride + offs_d * o_dim_stride
    tl.store(Ouput + offs_out, acc / d_i)

@torch.no_grad()
def flash_decode_stage2(
    mid_o, mid_o_logexpsum, # 存储每个批次、每个头、每个分区的中间分数输出及 log(sum(exp(scores)))
	atten_output,           # attention 输出首地址
	b_seq_len,  	            # kv cache 在 seq_len 维度的长度向量
    PARTITION_SIZE
):	
	batchs, num_heads, head_dim = mid_o.shape[0], mid_o.shape[1], mid_o.shape[-1]
	grid = (batchs, num_heads)
	
	_flash_decoding_stage2_kernel[grid](
		mid_o,  	     # [batch, head, seq_block_num, head_dim]
		mid_o_logexpsum, # [batch, head, seq_block_num]
		atten_output,           # attention 输出首地址
		*mid_o.stride(),
		*mid_o_logexpsum.stride(),
		*atten_output.stride(),
		b_seq_len,   # TODO 支持 PagedAttention 和连续批处理
		BLOCK_DMODEL = head_dim,
		BLOCK_SEQ = PARTITION_SIZE, # type: ignore	
		num_warps = 4,
		num_stages = 2,
	)

@torch.no_grad()
def flash_decoding(
    q, 			 # q 查询向量，形状为 [bsz, num_head, head_dim]
    k_cache, v_cache, 	     # 键/值向量缓存，形状为 [max_tokens, kv_num_head, head_dim]
    qk_scale,
    b_start_loc, b_seq_len, # start locations and sequence lengths for kv cache in a batch
    max_actual_seq_len
):
	# q.view(-1, num_heads, head_dim)
	assert q.shape[-1] == k_cache.shape[-1] == v_cache.shape[-1]
	PARTITION_SIZE = 128
	batchs, num_heads, head_dim = q.shape # decode 阶段 q 的 seq_len = 1, 

	# 最大可用分区数量计算
	max_num_partitions = (max_actual_seq_len + PARTITION_SIZE -1) // PARTITION_SIZE

	# mid_o: 存储每个批次、每个头、每个分区的中间输出
	mid_o = torch.empty((batchs, num_heads, max_num_partitions, head_dim), dtype=torch.float32, device=q.device)
	# 存储每个批次、每个头、每个分区的 log(sum(exp(scores)))，用于后续 decode_stage2 的归一化
	mid_o_logexpsum = torch.empty((batchs, num_heads, max_num_partitions), dtype=torch.float32, device=q.device)

	# decode stage 1: attention in partitions
	flash_decode_stage1(q, k_cache, v_cache, qk_scale, b_start_loc, b_seq_len, max_actual_seq_len, mid_o, mid_o_logexpsum, PARTITION_SIZE)
	# print(detect_nan(mid_o))
	# print(detect_nan(mid_o_logexpsum))
	
	# decode stage 2: reduction among partitions
	atten_output = torch.empty_like(q)

	flash_decode_stage2(mid_o, mid_o_logexpsum, atten_output, b_seq_len, PARTITION_SIZE)

	return atten_output


def _naive_attention(q, k, v):
    import math
    head_dim = q.shape[-1]
    q = q.transpose(0, 1)  #(nhead, 1, head_dim)
    k = k.transpose(0, 1)  #(nhead, seqlen, head_dim)
    v = v.transpose(0, 1)  #(nhead, seqlen, head_dim)
    scores = torch.matmul(q, k.transpose(1, 2)) / math.sqrt(head_dim)
    scores = torch.nn.functional.softmax(scores.float(), dim=-1).to(q.dtype)
    output = torch.matmul(scores, v).transpose(0, 1).contiguous() #(1, nhead, head_dim)
    return output

def torch_attention_with_kvcache(q, k_cache, v_cache, b_start_loc, b_seq_len):
    out = torch.empty_like(q)
    Z = q.shape[0]
    for i in range(Z):
        start = b_start_loc[i]
        end = start + b_seq_len[i]
        qi = q[i:i+1]            #(1, nhead, head_dim)
        ki = k_cache[start:end]  #(seqlen, nhead, head_dim)
        vi = v_cache[start:end]  #(seqlen, nhead, head_dim)
        oi = _naive_attention(qi, ki, vi)
        out[i:i+1] = oi
    return out

if __name__ == "__main__":
    torch.manual_seed(0)
    # inputs
    batch, head, head_dim = 6, 8, 256
    qk_scale = 1.0 / (head_dim ** 0.5)
    expand = 1
    max_input_len = 1024 * expand
    dtype = torch.float16
    q = torch.randn((batch, head, head_dim), device='cuda', dtype=dtype)
    k_cache = torch.randn((16 * 10000, head, head_dim), device='cuda', dtype=dtype)
    v_cache = torch.randn((16 * 10000, head, head_dim), device='cuda', dtype=dtype)
    # meta data for kv cache
    b_start_loc = torch.tensor([0, 1024, 2048, 3072, 4096, 5120], dtype=torch.int32, device="cuda") * expand
    b_seq_len = torch.tensor([512, 1024, 512, 1024, 512, 512], dtype=torch.int32, device="cuda") * expand
    # compute attention
    triton_output = flash_decoding(q, k_cache, v_cache, qk_scale, b_start_loc, b_seq_len, max_input_len)
    torch_output = torch_attention_with_kvcache(q, k_cache, v_cache, b_start_loc, b_seq_len)
    print(f'The maximum difference between torch and triton is {torch.max(torch.abs(torch_output - triton_output))}')
    # benchmark 
    print('torch:', triton.testing.do_bench(lambda: torch_attention_with_kvcache(q, k_cache, v_cache, b_start_loc, b_seq_len)))
    print('triton:', triton.testing.do_bench(lambda: flash_decoding(q, k_cache, v_cache, qk_scale, b_start_loc, b_seq_len, max_input_len)))