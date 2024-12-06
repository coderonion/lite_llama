import torch, json, time, logging
from pathlib import Path
import torch.nn as nn

from transformers import LlavaConfig
from accelerate import init_empty_weights, load_checkpoint_and_dispatch

from .mem_manager import ComputeMaxAvailableBlocks, KVCacheMemoryManager

from .cuda_graph import ModelRunner
from .executor_struct import AttentionInfo
from ..models.model_config import LlamaConfig, Qwen2Config
from .weight_convert import convert_llama_torch_to_litellama, \
                            convert_llavallama_hf_to_litellama, \
                            convert_qwen2_hf_to_litellama


logger = logging.getLogger(__name__)

    
def get_conversion_func(model_type: str):
    """
    根据模型类型获取相应的权重转换函数。

    参数:
        model_type (str): 模型类型。

    返回:
        function: 相应的权重转换函数，如果不支持则返回 None。
    """
    conversion_funcs = {
        "llama": convert_llama_torch_to_litellama,
        "qwen2": convert_qwen2_hf_to_litellama,
        "llava": convert_llavallama_hf_to_litellama,
    }
    return conversion_funcs.get(model_type.lower())
    
class ModelExecutor:
    # 定义类属性
    model_config = None
    model = None
    # model_runner_config = ModelRunnerConfig
    atten_info = AttentionInfo

    # 通过静态方法 build 将类属性当作默认配置使用
    @staticmethod
    def build(
        checkpoints_dir: str, 
        max_seq_len: int,
        max_gpu_num_blocks: None, 
        load_model: bool = True, 
        triton_weight: bool = True,
        compiled_model: bool = False, 
        device: str = "cuda", 
    ):
        """
        构建 ModelExecutor 实例, 加载模型、分词器和初始化推理信息结构体 atten_info。

        参数:
            checkpoints_dir (str): 模型检查点目录路径。
            load_model (bool): 是否加载模型权重。
            max_seq_len (int): 最大序列长度。
            device (str): 设备类型（'cuda'或'cpu'）。

        返回:
            ModelExecutor: 初始化后的 ModelExecutor 实例。
        """            
        model_config = ModelExecutor._load_model_config(checkpoints_dir, max_seq_len, device=device)
        # model = ModelExecutor._accelerate_load_weight(model_config, checkpoints_dir)
        model = ModelExecutor._load_model_weight(model_config, checkpoints_dir, load_model, triton_weight, device=device) # 加载权重后的模型

        return ModelExecutor(model_config, model, max_gpu_num_blocks, compiled_model, device)

    @staticmethod
    def _accelerate_load_weight(model_config, checkpoints_dir, load_model = True, triton_weight=True, device="cuda"):
        with init_empty_weights():
            model = ModelExecutor._initialize_model(model_config, device=device)

        # 假设 model 是使用 init_empty_weights 初始化的空模型
        model = load_checkpoint_and_dispatch(model, checkpoints_dir, device_map="auto", dtype=torch.float16 )

        # 将模型转换为半精度, 并验证抓换
        model.to(device)
        model.half()
        for param in model.parameters():
            assert param.dtype == torch.float16, "Model parameters are not in FP16"
        logger.info("Converted model to half precision (FP16)")

        return model
    
    @staticmethod
    def _load_model_weight(model_config, checkpoints_dir, load_model = True, triton_weight=True, device="cuda"):
        start_time = time.time()
        hf_sd = None
            
        # 初始化模型
        with init_empty_weights():
            model = ModelExecutor._initialize_model(model_config, device=device)
            state_dict = None
        
        if load_model:
            checkpoints = sorted(Path(checkpoints_dir).glob("*.pth"))
            assert len(checkpoints) > 0, f"no checkpoint files found in {checkpoints_dir}"
            ckpt_path = str(checkpoints[0])
            logger.debug("type(ckpt_path) ", type(ckpt_path))
            logger.info(f'Loading checkpoint "{ckpt_path}"')
            # 使用 torch.load 加载权重文件。torch.load 可以根据需要将权重加载到指定的设备上
            state_dict = torch.load(ckpt_path, mmap=True, weights_only=True, map_location=device)
        else:
            conversion_func = get_conversion_func(model_config.model_type)
            if conversion_func is None:
                logger.error(f"不支持的模型类型: {model_config.model_type}")
                raise ValueError(f"Unsupported model type: {model_config.model_type}")
            state_dict = conversion_func(checkpoints_dir, hf_sd, model_config)
            logger.info(f" 权重名称转换完成，耗时 {time.time() - start_time:.2f} 秒。")
            
        model.load_state_dict(state_dict, strict=True, assign=True) # 将加载的 state_dict 应用到模型实例中。
        model.eval()
        logger.info(f" Loaded state dict in {time.time() - start_time:.2f}s")

        # 将模型转换为半精度, 并验证转换
        model.half().to(device)
        for param in model.parameters():
            assert param.dtype == torch.float16, "Model parameters are not in FP16"
        logger.info(" Converted model to half precision (FP16)")
        
        return model
    
    @staticmethod
    def _initialize_model(model_config, device: str) -> nn.Module:
        """
        根据配置初始化模型并将其移动到指定设备。

        参数:
            model_config (LlamaConfig): 自定义模型的配置参数。
            device (str): 设备类型（'cuda'或'cpu'）。

        返回:
            nn.Module: 初始化后的模型。
        """
        model_type = model_config.model_type.lower()
        logger.info(f" 初始化模型类型 '{model_type}' 并移动到设备 '{device}'...")
        if model_type == "llama":
            from ..models.llama import LlamaModel
            model = LlamaModel(model_config)
        elif model_type == "qwen2":
            from ..models.qwen2 import Qwen2Model
            model = Qwen2Model(model_config)
        elif model_type == "llava":
            from ..models.llava import LlavaLlama
            model = LlavaLlama(model_config)
        else:
            logger.error(f"不支持的模型类型: {model_type}")
            raise ValueError(f"Unsupported model type: {model_type}")

        logger.info(f" 模型已初始化并移动到设备 '{device}'。")
        return model

    @staticmethod
    def _load_model_config(checkpoints_dir, max_seq_len, device="cuda"):
        
        params_path = Path(checkpoints_dir) / "config.json" # 定义模型配置文件
        assert params_path.exists(), f"params.json not found in {checkpoints_dir}"
        try:
            with open(params_path, "r") as f:
                params = json.load(f)
        except FileNotFoundError:
            logger.error(f"配置文件 {params_path} 不存在，请检查路径是否正确。")
            raise

        if params["model_type"]== "llama":
            model_config: LlamaConfig = LlamaConfig.from_dict(params)
        elif params["model_type"] == "qwen2":
            model_config: Qwen2Config = Qwen2Config(
                params, 
                max_seq_len = max_seq_len,
                device=device
            )
        elif params["model_type"] == "llava":
            model_config = LlavaConfig.from_pretrained(checkpoints_dir)

        return model_config

    def __init__(self, model_config, model, max_gpu_num_blocks=None, compiled_model=False, device="cuda"):
        self.model_config = model_config

        if isinstance(model_config, LlavaConfig):
            self.llm_config = LlamaConfig.from_dict(model_config.text_config.to_dict())
        else:
            self.llm_config = model_config

        self.model_type = model_config.model_type
        self.model = model

        self.compiled_model = False
        self.model_runner = None
        
        if max_gpu_num_blocks:
            self.kv_mem_manager = self._init_mem_manager(max_gpu_num_blocks)
        else:
            max_gpu_num_blocks, self.max_gpu_num_tokens = self._get_max_avaliable_tokens(gpu_memory_utilization=0.9, block_size=1)
            self.kv_mem_manager = self._init_mem_manager(max_gpu_num_blocks, block_size=1)
        
        if compiled_model:
            self.apply_cuda_graph() # 调用 cuda graph 优化

        self.gpu_kv_buffer = self.kv_mem_manager.gpu_kv_buffer
        self.atten_info = AttentionInfo() # 创建 AttentionInfo 实例
        self.atten_info.kv_buffer = self.kv_mem_manager.gpu_kv_buffer

    def _get_max_avaliable_tokens(self, gpu_memory_utilization=0.9, block_size=1):
        avaliable_blocks = ComputeMaxAvailableBlocks(
            num_layers = self.llm_config.num_layers, 
            hidden_size = self.llm_config.hidden_size, 
            num_heads = self.llm_config.num_heads, 
            num_kv_heads = self.llm_config.num_kv_heads, 
            gpu_memory_utilization = gpu_memory_utilization, 
            block_size = block_size,
        )
        max_gpu_num_blocks = avaliable_blocks.compute_num_available_blocks()
        max_gpu_num_tokens = max_gpu_num_blocks * block_size

        return max_gpu_num_blocks, max_gpu_num_tokens
    
    def _init_mem_manager(self, gpu_num_blocks, block_size=1, dtype=torch.float16,  device="cuda"):
        kv_mem_manager = KVCacheMemoryManager(
            num_layers = self.llm_config.num_layers,
            num_kv_heads = self.llm_config.num_kv_heads,
            head_dim = self.llm_config.head_dim,

            gpu_num_blocks = gpu_num_blocks,
            block_size = block_size,
            dtype = dtype,
            device=device
        )

        return kv_mem_manager

    def apply_cuda_graph(self, ):
        """应用 cuda graph 优化
        参数:
            - input_ids: 输入 tokens id 列表, shape: (batch_size, seq_len)
            - prev_pos: 当前处于第几轮迭代循环, 生成第几个 token
        """
        # TODO: 修复支持多模态模型配置问题的错误
        max_gpu_num_blocks, _ = self._get_max_avaliable_tokens(gpu_memory_utilization=0.9, block_size=1)
        
        kv_mem_manager = self._init_mem_manager(
            max_gpu_num_blocks, block_size=1, 
            dtype=torch.float16,  device="cuda"
        )
        self.model_runner = ModelRunner(
            self.model, 
            self.llm_config, 
            max_gpu_num_blocks, 
            kv_mem_manager
        )
        self.model_runner.capture_decode_graph()

        return  max_gpu_num_blocks, kv_mem_manager

    def forward(self, input_ids, prev_pos, image_tensor=None):
        batch_size, seq_len = input_ids.shape # 静态批处理, batch 中每个请求的 seq_len 都相等
        if seq_len > 1:
            # 一次性分配最大所需 kv cache. seq0: [token0, token1, token2, token3,], seq1: [token0, token1, token2, token3,]
            need_size = batch_size * (seq_len)
            
            if self.model_type == "llava":
                image_size = self.model_config.vision_config.image_size
                pathch_size = self.model_config.vision_config.patch_size
                number_patchs = image_size // pathch_size
                need_size += number_patchs * number_patchs - 1
            alloc_mem = self.kv_mem_manager.alloc_contiguous_kvcache(need_size)
            
            if alloc_mem is not None:
                select_index = alloc_mem[0]
            else:
                select_index, _, _  = self.kv_mem_manager.alloc_kvcache(need_size)
            self.atten_info.select_index = select_index
        else:
            alloc_mem = self.kv_mem_manager.alloc_contiguous_kvcache(batch_size)
            if alloc_mem is not None:
                decode_index = alloc_mem[0]
            else:
                decode_index, _, _  = self.kv_mem_manager.alloc_kvcache(batch_size)
            self.atten_info.decode_index = decode_index

        if self.model_type == "llava":
            logits = self.model.forward(input_ids, prev_pos, self.atten_info, image_tensor)
        else:
            logits = self.model.forward(input_ids, prev_pos, self.atten_info)
        
        if seq_len == 1 and self.atten_info.select_index.numel() > 0:
            select_index = torch.cat([self.atten_info.select_index.view(batch_size, -1),
                                     self.atten_info.decode_index.view(batch_size, -1)], dim=1).view(-1)
            self.atten_info.select_index = select_index
        
        return logits, self.atten_info.select_index