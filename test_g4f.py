#!/usr/bin/env python3
"""
Diagnostic script to test g4f providers directly
Run this inside the container to see what's actually failing
"""
import asyncio
import sys
import g4f
from g4f.client import Client

async def test_provider(provider_class, provider_name, model):
    """Test a single provider"""
    print(f"\n{'='*60}")
    print(f"Testing {provider_name} with model {model}")
    print(f"{'='*60}")
    
    try:
        client = Client(provider=provider_class)
        print(f"✓ Client created")
        
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            messages=[{"role": "user", "content": "Say 'test successful' and nothing else"}],
            timeout=30,
        )
        
        if response and response.choices and response.choices[0].message.content:
            content = response.choices[0].message.content
            print(f"✅ SUCCESS: {content[:100]}")
            return True
        else:
            print(f"❌ FAILED: Empty response")
            return False
            
    except Exception as e:
        print(f"❌ FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False

async def main():
    """Test all configured providers"""
    try:
        print(f"g4f version: {g4f.__version__}")
    except AttributeError:
        print(f"g4f installed (version unknown)")
    print(f"Python version: {sys.version}")
    
    # Match the host-side probe exactly
    providers = [
        (g4f.Provider.MetaAI, "MetaAI", "gpt-3.5-turbo"),
        (g4f.Provider.MetaAI, "MetaAI", "meta-llama/Meta-Llama-3.1-70B-Instruct"),
        (g4f.Provider.Gemini, "Gemini", "gemini-2.0-flash-exp"),
    ]
    
    results = []
    for provider_class, name, model in providers:
        success = await test_provider(provider_class, name, model)
        results.append((name, success))
    
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, success in results:
        status = "✅ WORKING" if success else "❌ FAILED"
        print(f"{name:20} {status}")
    
    if any(success for _, success in results):
        print("\n✅ At least one provider is working!")
        return 0
    else:
        print("\n❌ ALL providers failed - check network/SSL/dependencies")
        return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
