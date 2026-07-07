# src/core/router.py
from typing import List, Tuple

# Define our virtual combo models and fallback chains
# Format of chain: (provider_name, target_model_name)
MODEL_ROUTING = {
    # Virtual Models
    "combo-smart": [
        ("openrouter", "anthropic/claude-3.5-sonnet"),
        ("gemini", "gemini-1.5-pro"),
    ],
    "combo-fast": [
        ("groq", "llama3-8b-8192"),
        ("gemini", "gemini-1.5-flash"),
    ],
    
    # Direct mappings with fallback alternatives
    "gemini-1.5-pro": [
        ("gemini", "gemini-1.5-pro"),
        ("openrouter", "google/gemini-pro"),
    ],
    "gemini-1.5-flash": [
        ("gemini", "gemini-1.5-flash"),
        ("openrouter", "google/gemini-flash"),
    ],
    "claude-3-5-sonnet": [
        ("openrouter", "anthropic/claude-3.5-sonnet"),
        ("gemini", "gemini-1.5-pro"),
    ],
    "llama3-8b": [
        ("groq", "llama3-8b-8192"),
        ("openrouter", "meta-llama/llama-3-8b-instruct"),
    ],
}

def resolve_fallback_chain(requested_model: str) -> List[Tuple[str, str]]:
    """
    Given a model name requested by the client, resolve the fallback chain of
    (provider, destination_model_name) to attempt.
    """
    # 1. Check if model exists in routing config
    if requested_model in MODEL_ROUTING:
        return MODEL_ROUTING[requested_model]
        
    # 2. Heuristic mapping if it's not a virtual model
    if requested_model.startswith("gemini-"):
        return [("gemini", requested_model), ("openrouter", f"google/{requested_model}")]
    elif requested_model.startswith("claude-"):
        return [("openrouter", f"anthropic/{requested_model}")]
    elif "llama" in requested_model.lower():
        # Try groq first as it is super fast, fallback to openrouter
        return [("groq", requested_model), ("openrouter", requested_model)]
        
    # 3. Default fallback (OpenRouter supports almost everything)
    return [("openrouter", requested_model)]
