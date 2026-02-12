import os
import asyncio
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Select
from dotenv import load_dotenv
from supabase import create_client, Client

# ==================== CONFIGURAÇÃO INICIAL ====================
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
PIX_KEY = os.getenv('PIX_KEY')
PIX_CITY = os.getenv('PIX_CITY', 'Sao Paulo')
PIX_NAME = os.getenv('PIX_NAME')
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID'))
CART_CATEGORY_ID = int(os.getenv('CART_CATEGORY_ID'))

MENSAGEM_POS_CONFIRMACAO = (
    "Para receber seu produto, abra um ticket e mande o comprovante e nome."
)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# ==================== FUNÇÕES AUXILIARES ====================
def is_admin(interaction: discord.Interaction) -> bool:
    return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)

def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = ((crc >> 8) | (crc << 8)) & 0xFFFF
        crc ^= byte
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 8) & 0xFFFF
        crc ^= ((crc & 0xFF) << 4) << 8
        crc ^= (crc >> 8) & 0xFF
    return crc

def gerar_pix_payload(valor: float, txid: str) -> str:
    txid = txid[:25]
    payload = "000201"
    merchant_info = f"0014br.gov.bcb.pix0108{PIX_KEY}"
    payload += f"26{len(merchant_info):02d}{merchant_info}"
    payload += "52040000"
    payload += "5303986"
    valor_str = f"{valor:.2f}".replace(".", "")
    payload += f"54{len(valor_str):02d}{valor_str}"
    payload += "5802BR"
    payload += f"59{len(PIX_NAME):02d}{PIX_NAME}"
    payload += f"60{len(PIX_CITY):02d}{PIX_CITY}"
    txid_block = f"05{len(txid):02d}{txid}"
    payload += f"62{len(txid_block):02d}{txid_block}"
    payload += "6304"
    crc = crc16_ccitt(payload.encode())
    payload += f"{crc:04X}"
    return payload

# ==================== VIEWS PERSISTENTES ====================
class ProdutoView(View):
    """Botões fixos na embed do produto."""
    def __init__(self, produto_id: int, tem_variacoes: bool):
        super().__init__(timeout=None)
        self.produto_id = produto_id

        if tem_variacoes:
            self.add_item(SelecionarVariacaoButton(produto_id))
        else:
            self.add_item(ComprarSemVariacaoButton(produto_id))

class SelecionarVariacaoButton(Button):
    def __init__(self, produto_id: int):
        super().__init__(label="🛒 Selecionar Variação", style=discord.ButtonStyle.primary, custom_id=f"var_{produto_id}")

    async def callback(self, interaction: discord.Interaction):
        # Busca variações do produto
        vars = supabase.table("product_variations").select("*").eq("product_id", self.produto_id).execute().data
        if not vars:
            await interaction.response.send_message("Este produto não possui variações no momento.", ephemeral=True)
            return

        options = []
        for v in vars:
            label = f"{v['nome']} - R$ {v['preco']:.2f}"
            options.append(discord.SelectOption(label=label[:100], value=str(v['id'])))

        select = Select(placeholder="Escolha a variação...", options=options)

        async def select_callback(select_interaction: discord.Interaction):
            var_id = int(select_interaction.data['values'][0])
            variacao = next((v for v in vars if v['id'] == var_id), None)
            if not variacao:
                await select_interaction.response.send_message("Erro ao obter variação.", ephemeral=True)
                return

            await self.criar_pedido(select_interaction, self.produto_id, variacao)

        select.callback = select_callback
        view = View()
        view.add_item(select)
        await interaction.response.send_message("Selecione a variação desejada:", view=view, ephemeral=True)

    async def criar_pedido(self, interaction: discord.Interaction, produto_id: int, variacao: dict):
        await interaction.response.defer(ephemeral=True)

        produto = supabase.table("products").select("*").eq("id", produto_id).execute().data[0]
        valor = variacao['preco']
        cargo_id = variacao.get('cargo_id') or produto['cargo_id']
        txid = f"{interaction.user.id}_{datetime.datetime.utcnow().timestamp()}"
        payload_pix = gerar_pix_payload(valor, txid)

        categoria = bot.get_channel(CART_CATEGORY_ID)
        if not categoria:
            await interaction.followup.send("Erro: categoria de carrinhos não encontrada.", ephemeral=True)
            return

        thread = await categoria.create_thread(
            name=f"pedido-{interaction.user.name[:20]}-{produto_id}",
            type=discord.ChannelType.private_thread
        )
        await thread.add_user(interaction.user)

        data = {
            "user_id": str(interaction.user.id),
            "product_id": produto_id,
            "variation_id": variacao['id'],
            "amount": valor,
            "status": "pending",
            "payment_id": txid,
            "thread_id": thread.id,
            "cargo_entregue": False
        }
        supabase.table("orders").insert(data).execute()

        embed_pedido = discord.Embed(
            title="🛒 Pedido Realizado",
            description=f"Produto: **{produto['nome']}**\nVariação: **{variacao['nome']}**\nValor: **R$ {valor:.2f}**",
            color=discord.Color.from_str(produto['cor_embed'])
        )
        embed_pedido.add_field(name="Chave Pix (copia e cola)", value=f"```{payload_pix}```", inline=False)
        embed_pedido.add_field(name="Instruções", value="Realize o pagamento via Pix. Após a confirmação você receberá seu cargo e instruções.", inline=False)
        await thread.send(content=f"{interaction.user.mention}", embed=embed_pedido)

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            embed_log = discord.Embed(
                title="🆕 Novo Pedido",
                description=f"**Cliente:** {interaction.user.mention}\n**Produto:** {produto['nome']}\n**Variação:** {variacao['nome']}\n**Valor:** R$ {valor:.2f}",
                color=discord.Color.blue(),
                timestamp=datetime.datetime.utcnow()
            )
            embed_log.set_footer(text=f"Pedido #{data['id'] if 'id' in data else '...'}")
            await log_channel.send(embed=embed_log)

        await interaction.followup.send(f"✅ Pedido criado! Acompanhe em {thread.mention}", ephemeral=True)

class ComprarSemVariacaoButton(Button):
    def __init__(self, produto_id: int):
        super().__init__(label="💳 Comprar", style=discord.ButtonStyle.success, custom_id=f"buy_{produto_id}")

    async def callback(self, interaction: discord.Interaction):
        produto = supabase.table("products").select("*").eq("id", self.produto_id).execute().data[0]
        if not produto:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return

        valor = produto['preco']
        cargo_id = produto['cargo_id']
        txid = f"{interaction.user.id}_{datetime.datetime.utcnow().timestamp()}"
        payload_pix = gerar_pix_payload(valor, txid)

        await interaction.response.defer(ephemeral=True)

        categoria = bot.get_channel(CART_CATEGORY_ID)
        if not categoria:
            await interaction.followup.send("Erro: categoria de carrinhos não encontrada.", ephemeral=True)
            return

        thread = await categoria.create_thread(
            name=f"pedido-{interaction.user.name[:20]}-{self.produto_id}",
            type=discord.ChannelType.private_thread
        )
        await thread.add_user(interaction.user)

        data = {
            "user_id": str(interaction.user.id),
            "product_id": self.produto_id,
            "variation_id": None,
            "amount": valor,
            "status": "pending",
            "payment_id": txid,
            "thread_id": thread.id,
            "cargo_entregue": False
        }
        supabase.table("orders").insert(data).execute()

        embed_pedido = discord.Embed(
            title="🛒 Pedido Realizado",
            description=f"Produto: **{produto['nome']}**\nValor: **R$ {valor:.2f}**",
            color=discord.Color.from_str(produto['cor_embed'])
        )
        embed_pedido.add_field(name="Chave Pix (copia e cola)", value=f"```{payload_pix}```", inline=False)
        embed_pedido.add_field(name="Instruções", value="Realize o pagamento via Pix. Após a confirmação você receberá seu cargo e instruções.", inline=False)
        await thread.send(content=f"{interaction.user.mention}", embed=embed_pedido)

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            embed_log = discord.Embed(
                title="🆕 Novo Pedido",
                description=f"**Cliente:** {interaction.user.mention}\n**Produto:** {produto['nome']}\n**Valor:** R$ {valor:.2f}",
                color=discord.Color.blue(),
                timestamp=datetime.datetime.utcnow()
            )
            await log_channel.send(embed=embed_log)

        await interaction.followup.send(f"✅ Pedido criado! Acompanhe em {thread.mention}", ephemeral=True)

# ==================== COMANDOS DE ADMIN ====================
@tree.command(name="criar_produto", description="[ADMIN] Cria um novo produto com embed no canal atual.")
@app_commands.describe(
    nome="Nome do produto",
    descricao="Descrição detalhada",
    preco="Preço (se não tiver variações, coloque o valor; se tiver variações, coloque 0)",
    cargo_id="ID do cargo entregue",
    thumbnail_url="URL da imagem pequena (canto superior)",
    banner_url="URL da imagem grande (centro)"
)
async def criar_produto(
    interaction: discord.Interaction,
    nome: str,
    descricao: str,
    preco: float,
    cargo_id: str,
    thumbnail_url: str,
    banner_url: str
):
    if not is_admin(interaction):
        return await interaction.response.send_message("Permissão negada.", ephemeral=True)

    data = {
        "nome": nome,
        "descricao": descricao,
        "preco": None if preco == 0 else preco,
        "cargo_id": int(cargo_id),
        "cor_embed": "#ffffff",
        "thumbnail_url": thumbnail_url,
        "banner_url": banner_url,
        "canal_id": interaction.channel_id,
        "mensagem_id": None
    }
    result = supabase.table("products").insert(data).execute()
    produto_id = result.data[0]['id']

    embed = discord.Embed(
        title=nome,
        description=descricao,
        color=discord.Color.from_str("#ffffff")
    )
    if preco and preco > 0:
        embed.add_field(name="Preço", value=f"R$ {preco:.2f}", inline=False)
    embed.set_thumbnail(url=thumbnail_url)
    embed.set_image(url=banner_url)

    tem_variacoes = (preco == 0)
    view = ProdutoView(produto_id, tem_variacoes)

    msg = await interaction.channel.send(embed=embed, view=view)
    supabase.table("products").update({"mensagem_id": msg.id}).eq("id", produto_id).execute()

    await interaction.response.send_message(f"✅ Produto criado! ID: {produto_id}", ephemeral=True)

@tree.command(name="adicionar_variacao", description="[ADMIN] Adiciona uma variação a um produto existente.")
@app_commands.describe(
    produto_id="ID do produto",
    nome="Nome da variação (ex: 3 dias)",
    preco="Preço da variação",
    cargo_id="ID do cargo (opcional, se diferente do produto)"
)
async def adicionar_variacao(
    interaction: discord.Interaction,
    produto_id: int,
    nome: str,
    preco: float,
    cargo_id: Optional[str] = None
):
    if not is_admin(interaction):
        return await interaction.response.send_message("Permissão negada.", ephemeral=True)

    data = {
        "product_id": produto_id,
        "nome": nome,
        "preco": preco,
        "cargo_id": int(cargo_id) if cargo_id else None
    }
    supabase.table("product_variations").insert(data).execute()

    # Atualiza a view do produto para incluir o botão de seleção
    produto = supabase.table("products").select("*").eq("id", produto_id).execute().data[0]
    if produto['mensagem_id'] and produto['canal_id']:
        canal = bot.get_channel(produto['canal_id'])
        if canal:
            try:
                msg = await canal.fetch_message(produto['mensagem_id'])
                embed = msg.embeds[0]
                view = ProdutoView(produto_id, tem_variacoes=True)
                await msg.edit(embed=embed, view=view)
            except:
                pass

    await interaction.response.send_message(f"✅ Variação '{nome}' adicionada ao produto {produto_id}.", ephemeral=True)

@tree.command(name="pedidos", description="[ADMIN] Lista pedidos pendentes.")
async def pedidos(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("Permissão negada.", ephemeral=True)

    pedidos = supabase.table("orders").select("*, products(nome), product_variations(nome)").eq("status", "pending").order("criado_em", desc=True).execute().data
    if not pedidos:
        return await interaction.response.send_message("Nenhum pedido pendente.", ephemeral=True)

    total = len(pedidos)

    def embed_pedido(index):
        p = pedidos[index]
        embed = discord.Embed(
            title=f"Pedido #{p['id']}",
            color=discord.Color.orange()
        )
        embed.add_field(name="Cliente", value=f"<@{p['user_id']}>", inline=True)
        embed.add_field(name="Produto", value=p['products']['nome'], inline=True)
        if p['variation_id']:
            embed.add_field(name="Variação", value=p['product_variations']['nome'], inline=True)
        embed.add_field(name="Valor", value=f"R$ {p['amount']:.2f}", inline=True)
        embed.add_field(name="Status", value=p['status'], inline=True)
        embed.add_field(name="Data", value=p['criado_em'][:10], inline=True)
        embed.set_footer(text=f"Página {index+1} de {total}")
        return embed

    class PedidosView(View):
        def __init__(self, pedidos_list):
            super().__init__(timeout=180)
            self.pedidos = pedidos_list
            self.index = 0

        @discord.ui.button(label="◀", style=discord.ButtonStyle.blurple)
        async def anterior(self, i: discord.Interaction, b: discord.ui.Button):
            if not is_admin(i):
                return await i.response.send_message("Permissão negada.", ephemeral=True)
            self.index = (self.index - 1) % total
            await i.response.edit_message(embed=embed_pedido(self.index), view=self)

        @discord.ui.button(label="▶", style=discord.ButtonStyle.blurple)
        async def proximo(self, i: discord.Interaction, b: discord.ui.Button):
            if not is_admin(i):
                return await i.response.send_message("Permissão negada.", ephemeral=True)
            self.index = (self.index + 1) % total
            await i.response.edit_message(embed=embed_pedido(self.index), view=self)

        @discord.ui.button(label="✅ Confirmar Pagamento", style=discord.ButtonStyle.success)
        async def confirmar(self, i: discord.Interaction, b: discord.ui.Button):
            if not is_admin(i):
                return await i.response.send_message("Permissão negada.", ephemeral=True)
            p = self.pedidos[self.index]
            supabase.table("orders").update({"status": "paid"}).eq("id", p['id']).execute()

            guild = i.guild
            member = guild.get_member(int(p['user_id']))
            if member:
                if p['variation_id']:
                    var = supabase.table("product_variations").select("cargo_id").eq("id", p['variation_id']).execute().data[0]
                    cargo_id = var['cargo_id'] or supabase.table("products").select("cargo_id").eq("id", p['product_id']).execute().data[0]['cargo_id']
                else:
                    cargo_id = supabase.table("products").select("cargo_id").eq("id", p['product_id']).execute().data[0]['cargo_id']
                role = guild.get_role(int(cargo_id))
                if role:
                    await member.add_roles(role)
                    supabase.table("orders").update({"cargo_entregue": True}).eq("id", p['id']).execute()

            if p['thread_id']:
                thread = bot.get_channel(int(p['thread_id']))
                if thread:
                    await thread.send(f"✅ **Pagamento confirmado!**\n{MENSAGEM_POS_CONFIRMACAO}")
                    await thread.edit(archived=True, locked=True)

            await i.response.send_message(f"Pedido #{p['id']} confirmado e cargo entregue.", ephemeral=True)
            self.pedidos.pop(self.index)
            if not self.pedidos:
                await i.edit_original_response(content="Nenhum pedido pendente.", embed=None, view=None)
            else:
                self.index = min(self.index, len(self.pedidos)-1)
                await i.edit_original_response(embed=embed_pedido(self.index), view=self)

        @discord.ui.button(label="❌ Cancelar Pedido", style=discord.ButtonStyle.danger)
        async def cancelar(self, i: discord.Interaction, b: discord.ui.Button):
            if not is_admin(i):
                return await i.response.send_message("Permissão negada.", ephemeral=True)
            p = self.pedidos[self.index]
            supabase.table("orders").update({"status": "cancelled"}).eq("id", p['id']).execute()
            if p['thread_id']:
                thread = bot.get_channel(int(p['thread_id']))
                if thread:
                    await thread.send("❌ Pedido cancelado.")
                    await thread.edit(archived=True, locked=True)
            await i.response.send_message(f"Pedido #{p['id']} cancelado.", ephemeral=True)
            self.pedidos.pop(self.index)
            if not self.pedidos:
                await i.edit_original_response(content="Nenhum pedido pendente.", embed=None, view=None)
            else:
                self.index = min(self.index, len(self.pedidos)-1)
                await i.edit_original_response(embed=embed_pedido(self.index), view=self)

    await interaction.response.send_message(embed=embed_pedido(0), view=PedidosView(pedidos))

@tree.command(name="dashboard", description="[ADMIN] Métricas de vendas.")
async def dashboard(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("Permissão negada.", ephemeral=True)

    total_pedidos = supabase.table("orders").select("id", count="exact").eq("status", "paid").execute().count
    paid_orders = supabase.table("orders").select("amount").eq("status", "paid").execute().data
    faturamento_total = sum(o['amount'] for o in paid_orders)
    hoje = datetime.date.today().isoformat()
    paid_hoje = supabase.table("orders").select("amount").eq("status", "paid").gte("criado_em", f"{hoje}T00:00:00").execute().data
    faturamento_hoje = sum(o['amount'] for o in paid_hoje)

    embed = discord.Embed(
        title="📊 Dashboard de Vendas",
        color=discord.Color.green()
    )
    embed.add_field(name="Total de Pedidos Pagos", value=str(total_pedidos), inline=False)
    embed.add_field(name="Faturamento Total", value=f"R$ {faturamento_total:.2f}", inline=False)
    embed.add_field(name="Faturamento Hoje", value=f"R$ {faturamento_hoje:.2f}", inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="editar_produto", description="[ADMIN] Edita um produto existente (breve).")
async def editar_produto(interaction: discord.Interaction, produto_id: int):
    await interaction.response.send_message("Em desenvolvimento. Use o SQL por enquanto.", ephemeral=True)

@tree.command