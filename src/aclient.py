import os
import discord
import asyncio
import logging
from typing import List, Dict, Optional

from src import personas
from src.log import logger
from src.providers import ProviderManager, ProviderType, ModelInfo
from utils.message_utils import send_split_message

from dotenv import load_dotenv
from discord import app_commands

load_dotenv()


class DiscordClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        
        self.tree = app_commands.CommandTree(self)
        
        # Initialize provider manager
        self.provider_manager = ProviderManager()
        
        # Set default provider and model
        default_provider = os.getenv("DEFAULT_PROVIDER", "free")
        try:
            self.provider_manager.set_current_provider(ProviderType(default_provider))
        except ValueError:
            logger.warning(f"Invalid default provider {default_provider}, using free")
            self.provider_manager.set_current_provider(ProviderType.FREE)
        
        self.current_model = os.getenv("DEFAULT_MODEL", "auto")

        # Image provider preferences (optional override)
        self.image_provider: Optional[ProviderType] = None
        self.image_model: Optional[str] = os.getenv("IMAGE_MODEL") or None
        env_image_provider = os.getenv("IMAGE_PROVIDER")
        if env_image_provider:
            try:
                self.image_provider = ProviderType(env_image_provider.lower())
            except ValueError:
                logger.warning(f"Invalid IMAGE_PROVIDER {env_image_provider}, ignoring")
        
        # Conversation management
        self.conversation_history = []
        self.current_channel = None
        self.current_persona = "standard"
        
        # Bot settings
        self.activity = discord.Activity(
            type=discord.ActivityType.listening, 
            name="/chat | /help | /provider"
        )
        self.isPrivate = False
        self.is_replying_all = os.getenv("REPLYING_ALL", "False") == "True"
        self.replying_all_discord_channel_id = os.getenv("REPLYING_ALL_DISCORD_CHANNEL_ID")
        
        # Load system prompt
        config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        prompt_path = os.path.join(config_dir, "system_prompt.txt")
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                self.starting_prompt = f.read()
        except FileNotFoundError:
            self.starting_prompt = ""
            logger.warning("system_prompt.txt not found")
        
        # Message queue for rate limiting
        self.message_queue = asyncio.Queue()
    
    async def process_messages(self):
        """Process queued messages"""
        while True:
            if self.current_channel is not None:
                while not self.message_queue.empty():
                    async with self.current_channel.typing():
                        message, user_message = await self.message_queue.get()
                        try:
                            await self.send_message(message, user_message)
                        except Exception as e:
                            logger.exception(f"Error while processing message: {e}")
                        finally:
                            self.message_queue.task_done()
            await asyncio.sleep(1)
    
    async def enqueue_message(self, message, user_message):
        """Add message to processing queue"""
        await message.response.defer(ephemeral=self.isPrivate) if hasattr(message, 'response') else None
        await self.message_queue.put((message, user_message))
    
    async def send_message(self, message, user_message):
        """Send response to user"""
        if hasattr(message, 'user'):  # Slash command
            author = message.user.id
        else:  # Regular message
            author = message.author.id
        
        try:
            response = await self.handle_response(user_message)
            response_content = f'> **{user_message}** - <@{str(author)}> \n\n{response}'
            await send_split_message(self, response_content, message)
        except Exception as e:
            logger.exception(f"Error while sending: {e}")
            error_msg = f"❌ Error: {str(e)}"
            if hasattr(message, 'followup'):
                await message.followup.send(error_msg)
            else:
                await message.channel.send(error_msg)
    
    async def send_start_prompt(self):
        """Send initial system prompt"""
        discord_channel_id = os.getenv("DISCORD_CHANNEL_ID")
        try:
            if self.starting_prompt and discord_channel_id:
                channel = self.get_channel(int(discord_channel_id))
                logger.info(f"Send system prompt with size {len(self.starting_prompt)}")
                
                response = await self.handle_response(self.starting_prompt)
                await channel.send(f"{response}")
                
                logger.info(f"System prompt response: {response}")
            else:
                logger.info("No starting prompt given or no Discord channel selected.")
        except Exception as e:
            logger.exception(f"Error while sending system prompt: {e}")
    
    async def handle_response(self, user_message: str) -> str:
        """Generate response using current provider"""
        # Add user message to history
        self.conversation_history.append({'role': 'user', 'content': user_message})
        
        # Better conversation management
        MAX_CONVERSATION_LENGTH = int(os.getenv("MAX_CONVERSATION_LENGTH", "20"))
        CONVERSATION_TRIM_SIZE = int(os.getenv("CONVERSATION_TRIM_SIZE", "8"))
        
        if len(self.conversation_history) > MAX_CONVERSATION_LENGTH:
            # Keep system prompts (first few messages) and recent context
            system_messages = [m for m in self.conversation_history[:3] if m['role'] == 'system']
            recent_messages = self.conversation_history[-CONVERSATION_TRIM_SIZE:]
            
            # Ensure we don't lose important context
            if system_messages:
                self.conversation_history = system_messages + recent_messages
            else:
                self.conversation_history = recent_messages
            
            logger.info(f"Trimmed conversation history to {len(self.conversation_history)} messages")
        
        # Get current provider
        provider = self.provider_manager.get_provider()
        
        try:
            # Generate response
            response = await provider.chat_completion(
                messages=self.conversation_history,
                model=self.current_model if self.current_model != "auto" else None
            )
            
            # Add to history
            self.conversation_history.append({'role': 'assistant', 'content': response})
            
            return response
            
        except Exception as e:
            logger.error(f"Provider error: {e}")
            # Try fallback to free provider
            if self.provider_manager.current_provider != ProviderType.FREE:
                logger.info("Falling back to free provider")
                try:
                    free_provider = self.provider_manager.get_provider(ProviderType.FREE)
                    response = await free_provider.chat_completion(
                        messages=self.conversation_history,
                        model=None
                    )
                    self.conversation_history.append({'role': 'assistant', 'content': response})
                    return f"{response}\n\n*⚠️ Fallback to free provider due to error*"
                except Exception as fallback_error:
                    logger.error(f"Fallback provider also failed: {fallback_error}")
                    # Return user-friendly error message
                    error_response = "❌ I'm having trouble processing your request right now. Please try again later or contact an administrator."
                    self.conversation_history.append({'role': 'assistant', 'content': error_response})
                    return error_response
            else:
                # Already using free provider, return error with more detail
                logger.error(f"Free provider failed with: {e}")
                error_response = "❌ The free provider is having issues. Please switch to a paid provider (OpenAI, Claude, Gemini, Grok, Perplexity) using `/provider`."
                self.conversation_history.append({'role': 'assistant', 'content': error_response})
                return error_response
    
    async def generate_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        provider_type: Optional[ProviderType] = None,
    ) -> str:
        """Generate image using requested provider, stored preference, or fallback to paid image providers."""

        # Choose provider priority: explicit param > stored preference > current provider
        chosen_provider_type = provider_type or self.image_provider
        if chosen_provider_type:
            provider = self.provider_manager.get_provider(chosen_provider_type)
        else:
            provider = self.provider_manager.get_provider()

        model_to_use = model or self.image_model
        
        if not provider.supports_image_generation():
            # Candidates: stored preference (if not used), env preference, then paid image providers
            candidates: List[ProviderType] = []

            # If we had an explicit provider_type that failed images, drop to next options
            if not provider_type and self.image_provider:
                candidates.append(self.image_provider)

            # Env preference already loaded into self.image_provider; include if not present
            preferred_env = os.getenv("IMAGE_PROVIDER", "").strip().lower()
            try:
                env_pt = ProviderType(preferred_env) if preferred_env else None
                if env_pt and env_pt not in candidates:
                    candidates.append(env_pt)
            except ValueError:
                pass

            # Paid providers that support images
            for p in [ProviderType.OPENAI, ProviderType.GEMINI]:
                if p not in candidates:
                    candidates.append(p)

            for candidate in candidates:
                try:
                    fallback_provider = self.provider_manager.get_provider(candidate)
                    if fallback_provider.supports_image_generation():
                        logger.info(f"Falling back to {candidate.value} for image generation")
                        return await fallback_provider.generate_image(prompt, model_to_use)
                except ValueError:
                    continue

            raise NotImplementedError("Image generation requires OpenAI or Gemini. Add OPENAI_KEY or GEMINI_KEY to .env or set IMAGE_PROVIDER.")
        
        return await provider.generate_image(prompt, model_to_use)

    def set_image_provider(self, provider_type: Optional[ProviderType], model: Optional[str] = None):
        self.image_provider = provider_type
        if model:
            self.image_model = model
    
    def reset_conversation_history(self):
        """Reset conversation and persona"""
        self.conversation_history = []
        self.current_persona = "standard"
        personas.current_persona = "standard"
    
    async def switch_persona(self, persona: str, user_id: Optional[str] = None) -> None:
        """Switch to a different persona"""
        self.reset_conversation_history()
        self.current_persona = persona
        personas.current_persona = persona
        
        # Add persona prompt to conversation (with permission check)
        persona_prompt = personas.get_persona_prompt(persona, user_id)
        self.conversation_history.append({'role': 'system', 'content': persona_prompt})
        
        # Get initial response with new persona
        await self.handle_response("Hello! Please confirm you understand your new role.")
    
    def get_current_provider_info(self) -> Dict:
        """Get information about current provider and model"""
        provider = self.provider_manager.get_provider()
        models = provider.get_available_models()
        
        return {
            "provider": self.provider_manager.current_provider.value,
            "current_model": self.current_model,
            "available_models": models,
            "supports_images": provider.supports_image_generation()
        }
    
    def switch_provider(self, provider_type: ProviderType, model: Optional[str] = None):
        """Switch to a different provider and optionally set model"""
        self.provider_manager.set_current_provider(provider_type)
        if model:
            self.current_model = model
        else:
            # Set to first available model or auto
            provider = self.provider_manager.get_provider()
            models = provider.get_available_models()
            self.current_model = models[0].name if models else "auto"


# Create singleton instance
discordClient = DiscordClient()