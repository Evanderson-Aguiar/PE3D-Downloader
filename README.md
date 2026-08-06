# PE3D-Downloader

Plugin para QGIS que localiza e baixa produtos do programa Pernambuco 3D (PE3D) a partir de uma área de interesse. O plugin trabalha com MDE, MDT e ortofotos, da escala 1:5000, organiza os arquivos, cria mosaicos virtuais e carrega no projeto somente a extensão escolhida pelo usuário.

## Recursos

- Seleção da área de interesse pela extensão do mapa, extensão de uma camada ou retângulo desenhado.
- Download de MDE, MDT e ORTOFOTO.
- Processamento produto por produto e em ordem geográfica contínua.
- Duas barras de progresso: progresso total e progresso do produto/fase atual.
- Downloads em segundo plano usando o gerenciador de tarefas do QGIS.
- Até três tentativas após falhas de conexão.
- Retomada de arquivos `.part` quando o servidor oferece suporte a HTTP Range.
- Cache validado por URL, tamanho, data de modificação e SHA-256.
- Extração segura, com limites de tamanho, quantidade de arquivos e taxa de compressão.
- Reutilização de extrações previamente validadas.
- Geração de mosaico VRT e recorte para a extensão da área de interesse.
- Opção de processar e carregar arquivos concluídos após cancelamento ou falha definitiva.
- Preservação das camadas que já estão abertas no projeto QGIS.

## Requisitos

- QGIS 3.16 ou superior, até a série 3.x.
- Acesso à internet para os downloads.
- Espaço em disco suficiente para armazenar os ZIPs e os TIFFs extraídos.

As bibliotecas QGIS, GDAL e PyQt utilizadas pelo plugin já fazem parte de uma instalação normal do QGIS. A biblioteca `requests` também precisa estar disponível no ambiente Python do QGIS.


## Guia de uso

### 1. Definir a área de interesse

Escolha uma das opções em **Fonte da área**:

- **Extensão da tela:** usa a extensão atualmente visível no mapa.
- **Extensão de camada:** usa o retângulo envolvente da camada selecionada.
- **Desenhar retângulo:** clique no botão correspondente e arraste o mouse sobre o mapa.

O SRC e as coordenadas da área escolhida são apresentados no campo de informações da AOI.

> A opção "Extensão de camada" usa a extensão retangular da camada, e não o contorno individual de suas feições.

### 2. Escolher a pasta de saída

Em **Saída**, escolha uma pasta com espaço disponível. Essa localização será lembrada nas próximas execuções.

### 3. Selecionar os produtos

Marque um ou mais produtos:

- **MDE:** Modelo Digital de Elevação.
- **MDT:** Modelo Digital do Terreno.
- **ORTOFOTO:** imagens ortorretificadas.

Somente os campos da grade correspondentes aos produtos selecionados são validados.

### 4. Executar

Clique em **Executar**. O plugin identifica as quadrículas que intersectam a AOI e processa os produtos na ordem MDE, MDT e ORTOFOTO, considerando somente os itens marcados.

Para cada produto, o fluxo é:

1. baixar todas as quadrículas de cima para baixo e, em cada linha, da esquerda para a direita;
2. validar e extrair os arquivos;
3. gerar o mosaico VRT completo;
4. criar um VRT recortado pela extensão da AOI;
5. carregar o resultado no projeto.

A barra superior representa o progresso total. A barra inferior e o texto de estado mostram o produto e a fase atuais.

## Cancelamento e falhas de internet

Ao clicar em **Cancelar**, o download atual é interrompido de forma controlada. Se houver ZIPs completos, o plugin pergunta se eles devem ser extraídos e carregados.

Quando a conexão cai:

1. o arquivo parcial é preservado com extensão `.part`;
2. o plugin faz até três tentativas com intervalos progressivos;
3. quando possível, o download continua a partir do ponto interrompido;
4. se a conexão não retornar, o plugin oferece processar os arquivos completos disponíveis.

Em uma execução futura, arquivos parciais poderão ser retomados e caches válidos serão reutilizados.

## Arquivos gerados

A pasta de saída contém subpastas por produto:

```text
MDE/
MDT/
ORTOFOTO/
```

Cada subpasta armazena os ZIPs, metadados de validação e diretórios `fid_<n>` com os TIFFs extraídos.

Os resultados recebem um identificador único por execução, por exemplo:

```text
PE3D_MDE_20260803_023000_123.vrt
PE3D_MDE_20260803_023000_123.source.vrt
PE3D_MDT_20260803_023000_123.vrt
PE3D_MDT_20260803_023000_123.source.vrt
PE3D_ORTOFOTO_20260803_023000_123.vrt
PE3D_ORTOFOTO_20260803_023000_123.source.vrt
```

O arquivo `.source.vrt` representa o mosaico completo e é utilizado pelo VRT recortado correspondente. Não o remova enquanto o resultado recortado estiver em uso.

## Suporte e desenvolvimento

- Autor: [Evanderson H. Aguiar](https://plugins.qgis.org/plugins/author/Evanderson%2520H.%2520Aguiar/)
- Mantenedor: [evanderson](https://plugins.qgis.org/plugins/user/evanderson/admin)
- Página e código-fonte: [GitHub](https://github.com/Evanderson-Aguiar/PE3D-Downloader)
- Problemas e sugestões: [GitHub Issues](https://github.com/Evanderson-Aguiar/PE3D-Downloader/issues)

Ao relatar um problema, informe a versão do QGIS, o produto selecionado, o SRC do projeto e as mensagens exibidas no log.

## Licença

Este projeto é distribuído sob a GNU General Public License, versão 2, de junho de 1991. Consulte o arquivo [LICENSE](LICENSE).
