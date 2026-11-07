#!/usr/bin/env Rscript
# Manuscript figures 3 and 5 for the CSEA paper. Backend: R (ggplot2 + patchwork).
#
# Figure contract
# ---------------
# F3  Core claim: the disclosure gap is mostly not the publisher's to close -- 73% of
#     it is forced by the schema and the package format.
#     Evidence chain: (a) the class-instance partition, which is an identity and so
#     carries the headline; (b) the same corpus per unit, which is the objection a
#     reviewer raises against class weighting and therefore earns its own panel.
#     Archetype: quantitative grid, hero on the left.
# F5  Core claim: an independent annotator reproduces the scanner's precision ordering,
#     and the three weakest classes must not carry absence claims.
#     Two precisions per class on one axis; the gaps are the divergences.
#     Archetype: quantitative single panel (dumbbell).
#
# Journal contract: 180 mm text width, 5 pt glyph floor, editable text (svglite +
# cairo_pdf), SVG/PDF/PNG, source data beside the script.
#
# Run:  Rscript make_figures.R

suppressPackageStartupMessages({
  library(ggplot2)
  library(patchwork)
  library(jsonlite)
  library(svglite)
  library(ragg)
  library(grid)
})

SKILL <- "C:/Users/Fan/.agents/skills/nature-figure/scripts"
# The alignment auditor is a backend-neutral QA tool (not a plotting device), so it
# runs under the bundled interpreter rather than whatever is on PATH.
PY_BIN <- "C:/Users/Fan/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"
source(file.path(SKILL, "panel_alignment.R"))

OUT <- "C:/Users/Fan/Downloads/1/mcp-audit/paper/csea/out"
FIG <- "C:/Users/Fan/Downloads/1/mcp-audit/paper/csea/figures"
dir.create(FIG, showWarnings = FALSE, recursive = TRUE)

MM <- 1 / 25.4

# One neutral family, one signal family: colour encodes ownership, not magnitude.
C_SCHEMA   <- "#4A6FA5"
C_ARTEFACT <- "#A8BBCE"
C_BEHAV    <- "#C2703D"
C_NEUTRAL  <- "#4D4D4D"
C_FAINT    <- "#8A8A8A"

theme_set(
  theme_classic(base_size = 6.5, base_family = "sans") +
    theme(
      axis.line   = element_line(linewidth = 0.35, colour = "black"),
      axis.ticks  = element_line(linewidth = 0.35, colour = "black"),
      axis.text   = element_text(size = 6.3, colour = "black"),
      axis.title  = element_text(size = 6.8, colour = "black"),
      plot.title  = element_text(size = 8.2, face = "bold", hjust = 0),
      panel.grid  = element_blank(),
      legend.text = element_text(size = 6.2),
      legend.title = element_blank()
    )
)

# Measure the rendered panel viewports directly. The bundled patchwork helper
# mis-resolves panel cells under patchwork 1.4 (it reports zero-width boxes even for
# a two-panel minimal example), so this measures the device box of each panel
# viewport instead and hands the same manifest schema to the backend-neutral auditor.
measure_panels <- function(plot, width_in, height_in) {
  probe <- tempfile(fileext = ".pdf")
  grDevices::pdf(probe, width = width_in, height = height_in, useDingbats = FALSE)
  on.exit({ grDevices::dev.off(); unlink(probe) }, add = TRUE)
  grid::grid.newpage()
  print(plot)
  grid::grid.force()
  listing <- grid::grid.ls(viewports = TRUE, grobs = FALSE, print = FALSE)
  names_vp <- unique(listing$name)
  panel_vps <- names_vp[grepl("^panel-[0-9]+\\.[0-9-]+$", names_vp)]
  if (!length(panel_vps)) {
    stop("no panel viewports found in the rendered patchwork", call. = FALSE)
  }
  panels <- lapply(seq_along(panel_vps), function(i) {
    grid::seekViewport(panel_vps[i], recording = FALSE)
    lo <- grid::deviceLoc(grid::unit(0, "npc"), grid::unit(0, "npc"), valueOnly = TRUE)
    hi <- grid::deviceLoc(grid::unit(1, "npc"), grid::unit(1, "npc"), valueOnly = TRUE)
    grid::upViewport(0)
    list(id = letters[i],
         bbox_pt = unname(c(lo$x * 72, lo$y * 72, hi$x * 72, hi$y * 72)))
  })
  list(schema_version = 1L, backend = "r-patchwork",
       figure = list(width_pt = width_in * 72, height_pt = height_in * 72),
       panels = panels,
       row_groups = list(list(id = "row-1", panels = unname(vapply(panels, `[[`, "", "id")))),
       exemptions = list())
}

save_pub_r <- function(plot, stem, width_mm, height_mm, multi_panel = FALSE) {
  w <- width_mm * MM
  h <- height_mm * MM
  if (multi_panel) {
    manifest <- measure_panels(plot, w, h)
    manifest_path <- file.path(FIG, paste0(stem, ".alignment-layout.json"))
    jsonlite::write_json(manifest, manifest_path, auto_unbox = TRUE, digits = 6)
    status <- system2(PY_BIN, c(file.path(SKILL, "audit_panel_alignment.py"),
                                shQuote(manifest_path),
                                "--json-out", shQuote(file.path(FIG, paste0(stem, ".alignment.json"))),
                                "--overlay-svg", shQuote(file.path(FIG, paste0(stem, ".alignment.svg"))),
                                "--tolerance-pt", "1.5",
                                "--gutter-tolerance-pt", "1.5",
                                "--strict"),
                       stdout = TRUE, stderr = TRUE)
    cat(paste(status, collapse = "\n"), "\n")
    if (!is.null(attr(status, "status")) && attr(status, "status") != 0) {
      stop("panel alignment gate failed", call. = FALSE)
    }
  }
  svglite(file.path(FIG, paste0(stem, ".svg")), width = w, height = h)
  print(plot)
  invisible(dev.off())
  # The base pdf() device writes plain text runs; cairo_pdf mis-encodes the text
  # matrix badly enough that geometry audits read merged bounding boxes.
  grDevices::pdf(file.path(FIG, paste0(stem, ".pdf")), width = w, height = h,
                 family = "sans", useDingbats = FALSE, onefile = FALSE)
  print(plot)
  invisible(dev.off())
  agg_png(file.path(FIG, paste0(stem, ".png")), width = w, height = h,
          units = "in", res = 1200, background = "white")
  print(plot)
  invisible(dev.off())
  cat("  wrote", stem, ".svg/.pdf/.png\n")
}

# ------------------------------------------------------------------ figure 3 --
figure3 <- function() {
  s <- fromJSON(file.path(OUT, "summary.json"))
  tot <- s$totals
  uw  <- s$unit_weighted
  n_units <- s$units

  buckets <- data.frame(
    label  = factor(c("schema", "package format", "publisher"),
                    levels = c("schema", "package format", "publisher")),
    value  = c(tot$n_schema, tot$n_artefact, tot$n_behavioural),
    colour = c(C_SCHEMA, C_ARTEFACT, C_BEHAV),
    stringsAsFactors = FALSE
  )
  buckets$share <- buckets$value / sum(buckets$value)
  buckets$xmax  <- cumsum(buckets$share)
  buckets$xmin  <- c(0, head(buckets$xmax, -1))
  buckets$xmid  <- (buckets$xmin + buckets$xmax) / 2
  buckets$ink   <- ifelse(buckets$label == "package format", "#1a1a1a", "white")

  structural <- (tot$n_schema + tot$n_artefact) / sum(buckets$value)

  pa <- ggplot() +
    geom_rect(data = buckets,
              aes(xmin = xmin, xmax = xmax, ymin = -0.10, ymax = 0.10, fill = colour),
              colour = "white", linewidth = 0.45) +
    geom_text(data = buckets, aes(x = xmid, y = 0, label = sprintf("%.1f%%", 100 * share)),
              colour = buckets$ink, size = 2.75, fontface = "bold") +
    geom_text(data = buckets,
              aes(x = xmid, y = -0.20,
                  label = paste0(label, "\n", format(value, big.mark = ","))),
              colour = c(C_SCHEMA, "#6C82A0", C_BEHAV), size = 2.25,
              lineheight = 1.15, vjust = 1) +
    # The claim is a span, so draw the span: a bracket over the two forced buckets.
    annotate("segment", x = 0, xend = 0, y = -0.46, yend = -0.41,
             colour = C_NEUTRAL, linewidth = 0.28) +
    annotate("segment", x = 0, xend = structural, y = -0.46, yend = -0.46,
             colour = C_NEUTRAL, linewidth = 0.28) +
    annotate("segment", x = structural, xend = structural, y = -0.46, yend = -0.41,
             colour = C_NEUTRAL, linewidth = 0.28) +
    annotate("text", x = structural / 2, y = -0.54,
             label = sprintf("italic(S[structural]) == %.3f", structural),
             parse = TRUE, size = 2.6, colour = C_NEUTRAL, vjust = 1) +
    annotate("text", x = structural / 2, y = -0.74,
             label = "forced by the instrument, not chosen by the publisher",
             size = 2.2, colour = C_NEUTRAL, fontface = "italic", vjust = 1) +
    scale_fill_identity() +
    scale_x_continuous(limits = c(0, 1), expand = c(0, 0)) +
    scale_y_continuous(limits = c(-0.88, 0.14), expand = c(0, 0)) +
    labs(title = "a  Where the gap comes from",
         subtitle = sprintf("%s omitted class-instances, weighted by class",
                            format(sum(buckets$value), big.mark = ","))) +
    theme_void(base_size = 6.5, base_family = "sans") +
    theme(plot.title = element_text(size = 8.2, face = "bold", hjust = 0),
          plot.subtitle = element_text(size = 6.3, colour = C_NEUTRAL, hjust = 0,
                                       margin = margin(t = 2.6, b = 4)),
          plot.title.position = "plot",
          plot.margin = margin(0, 2, 0, 0))

  units <- data.frame(
    label = factor(c("all structural", "mixed", "all publisher-side", "no omission"),
                   levels = c("all structural", "mixed", "all publisher-side",
                              "no omission")),
    value = c(uw$fully_structural, uw$mixed, uw$fully_behavioural, uw$no_omission),
    colour = c(C_SCHEMA, "#D9A57F", C_BEHAV, "#D9D9D9"),
    stringsAsFactors = FALSE
  )
  units$share <- 100 * units$value / n_units

  publisher_side <- 100 * (uw$mixed + uw$fully_behavioural) / n_units

  pb <- ggplot(units, aes(y = label, x = share)) +
    geom_col(aes(fill = colour), width = 0.46) +
    geom_text(aes(label = sprintf("%.1f%%  (%s)", share,
                                  format(value, big.mark = ","))),
              hjust = 0, nudge_x = 1.4, size = 2.25, colour = C_NEUTRAL) +
    scale_fill_identity() +
    scale_x_continuous(limits = c(0, 54), expand = c(0, 0)) +
    scale_y_discrete(limits = rev) +
    labs(title = "b  The same corpus, one unit at a time",
         subtitle = sprintf("%.1f%% of units carry a publisher-side omission",
                            publisher_side),
         x = sprintf("share of %s units (%%)", format(n_units, big.mark = ",")),
         y = NULL) +
    theme_classic(base_size = 6.5, base_family = "sans") +
    theme(plot.title = element_text(size = 8.2, face = "bold", hjust = 0),
          plot.subtitle = element_text(size = 6.3, colour = C_NEUTRAL, hjust = 0,
                                       margin = margin(t = 2.6, b = 4)),
          plot.title.position = "plot",
          axis.line.y = element_blank(),
          axis.ticks.y = element_blank(),
          axis.text.y = element_text(size = 6.6),
          axis.title.x = element_text(size = 6.8),
          plot.margin = margin(2, 4, 0, 3))

  fig <- (pa | pb) + plot_layout(widths = c(1, 1))
  save_pub_r(fig, "fig3_decomposition", 180, 60, multi_panel = TRUE)
}

# ------------------------------------------------------------------ figure 5 --
figure5 <- function() {
  v <- fromJSON(file.path(OUT, "validation.json"))
  cls <- v$classes

  # Table 2 of the manuscript: hand-adjudicated precision, 40 matches per class.
  table2 <- c(env_access = 100.0, network_egress = 87.5, db_access = 75.0,
              subprocess = 92.5, credential_paths = 10.0, fs_destructive = 35.0,
              arbitrary_code = 5.0, unsafe_deserialize = 2.5)

  rows <- data.frame(
    name = names(table2),
    prec = as.numeric(table2),
    stringsAsFactors = FALSE
  )
  rows$conf <- 100 * vapply(rows$name, function(k) {
    d <- cls[[k]]
    if (is.null(d) || d$detected == 0) NA_real_ else d$confirm_rate
  }, numeric(1))
  rows$n <- vapply(rows$name, function(k) {
    d <- cls[[k]]
    if (is.null(d)) 0L else as.integer(d$detected)
  }, integer(1))
  rows <- rows[!is.na(rows$conf) & rows$n > 0, ]
  rows <- rows[order(-rows$prec), ]
  rows$y <- rev(seq_len(nrow(rows)))

  d <- rows
  lab_y <- setNames(d$y, d$name)

  p <- ggplot(d) +
    geom_segment(aes(x = conf, xend = prec, y = y, yend = y),
                 colour = "#CFCFCF", linewidth = 0.55, lineend = "round") +
    geom_point(aes(x = conf, y = y), shape = 21, size = 1.9, fill = "white",
               colour = C_BEHAV, stroke = 0.45) +
    geom_point(aes(x = prec, y = y), shape = 16, size = 1.9, colour = C_SCHEMA) +
    geom_text(aes(x = 110, y = y, label = format(n, big.mark = ",")),
              hjust = 1, size = 2.15, colour = C_FAINT) +
    # Only the two divergences that matter are annotated; labelling every delta
    # would reproduce the crowding this chart exists to avoid.
    annotate("text", x = (d$conf[d$name == "subprocess"] + d$prec[d$name == "subprocess"]) / 2,
             y = lab_y[["subprocess"]] + 0.30, label = "-33 pts",
             size = 2.15, colour = "#6B6B6B") +
    annotate("text", x = (d$conf[d$name == "credential_paths"] + d$prec[d$name == "credential_paths"]) / 2,
             y = lab_y[["credential_paths"]] - 0.34, label = "+13 pts",
             size = 2.15, colour = "#6B6B6B") +
    scale_x_continuous(limits = c(-1, 113), breaks = c(0, 25, 50, 75, 100),
                       expand = c(0, 0)) +
    scale_y_continuous(limits = c(0.4, nrow(d) + 0.6),
                       breaks = d$y, labels = d$name, expand = c(0, 0)) +
    labs(title = "Confirmation reproduces the precision ordering",
         subtitle = sprintf("%s stratified units; the number at right is detected instances",
                            format(v$n, big.mark = ",")),
         x = "precision (%)", y = NULL) +
    theme_classic(base_size = 6.5, base_family = "sans") +
    theme(plot.title = element_text(size = 8.2, face = "bold", hjust = 0),
          plot.subtitle = element_text(size = 6.3, colour = C_NEUTRAL, hjust = 0,
                                       margin = margin(t = 2.6, b = 4)),
          plot.title.position = "plot",
          axis.line.y = element_blank(),
          axis.ticks.y = element_blank(),
          axis.text.y = element_text(size = 6.6),
          axis.title.x = element_text(size = 6.8),
          plot.margin = margin(2, 6, 0, 3))

  save_pub_r(p, "fig5_precision_confirmation", 128, 74, multi_panel = FALSE)
}


# ------------------------------------------------------------------ figure 1 --
# A schematic, so every element is placed in explicit data coordinates: no scale
# can re-flow the boxes, and the abstention path is drawn as a path rather than
# described in prose.
figure1 <- function() {
  bx <- function(x0, x1, y0, y1, label, sub = NULL, fill = "#F4F6F8",
                 colour = "#3A3A3A", size = 2.35, sub_size = 1.95) {
    g <- list(annotate("rect", xmin = x0, xmax = x1, ymin = y0, ymax = y1,
                       fill = fill, colour = colour, linewidth = 0.3))
    if (is.null(sub)) {
      g <- c(g, list(annotate("text", x = (x0 + x1) / 2, y = (y0 + y1) / 2,
                              label = label, size = size, colour = "#1A1A1A")))
    } else {
      g <- c(g, list(
        annotate("text", x = (x0 + x1) / 2, y = (y0 + y1) / 2 + 1.9, label = label,
                 size = size, fontface = "bold", colour = "#1A1A1A"),
        annotate("text", x = (x0 + x1) / 2, y = (y0 + y1) / 2 - 2.0, label = sub,
                 size = sub_size, colour = C_NEUTRAL, lineheight = 0.95)))
    }
    g
  }
  ar <- function(x0, x1, y, col = "#6B6B6B") {
    annotate("segment", x = x0, xend = x1, y = y, yend = y, colour = col,
             linewidth = 0.3,
             arrow = arrow(length = unit(0.05, "inches"), type = "closed"))
  }

  stages <- list(
    c(2, 40, "7 registries", "8 fetch days\n158,631 records"),
    c(46, 88, "frozen snapshot", "734,035 observations"),
    c(94, 132, "identity alignment", "100,688 listings\n22,104 cross-source"),
    c(138, 178, "evidence", "declaration · code · time")
  )
  p <- ggplot()
  for (st in stages) p <- p + bx(as.numeric(st[1]), as.numeric(st[2]), 76, 88, st[3], st[4])
  p <- p + ar(40.5, 45.5, 82) + ar(88.5, 93.5, 82) + ar(132.5, 137.5, 82)

  # guards: pass flows down the middle, failure leaves to the right
  guards <- data.frame(
    y = c(62, 53, 44, 35, 26, 17, 8),
    guard = c("S0  identity resolved", "S1  evidence collected",
              "S2  evidence analysable", "S3  declaration present",
              "S4  decision procedure exists", "S5  sources contradictory",
              "S6  compare"),
    fail = c("UNRESOLVED · IDENTITY_AMBIGUOUS",
             "INSUFFICIENT · CODE_GONE / UNAVAILABLE",
             "INSUFFICIENT · CODE_OPAQUE",
             "INSUFFICIENT · NO_DECLARATION",
             "INSUFFICIENT · CHANNEL_ABSENT",
             "CONFLICT · SOURCE_DISAGREEMENT",
             "SUPPORTED · PARTIAL · UNSUPPORTED"),
    stringsAsFactors = FALSE
  )
  for (i in seq_len(nrow(guards))) {
    y <- guards$y[i]
    last <- i == nrow(guards)
    p <- p + annotate("rect", xmin = 2, xmax = 64, ymin = y - 3.2, ymax = y + 3.2,
                      fill = if (last) "#E8EEF5" else "white",
                      colour = "#3A3A3A", linewidth = 0.3)
    p <- p + annotate("text", x = 4, y = y, label = guards$guard[i], size = 2.2,
                      colour = "#1A1A1A", hjust = 0)
    p <- p + annotate("rect", xmin = 96, xmax = 178, ymin = y - 3.2, ymax = y + 3.2,
                      fill = if (last) "#E8EEF5" else "#FBF7F4",
                      colour = if (last) "#3A3A3A" else "#C9C9C9", linewidth = 0.3)
    p <- p + annotate("text", x = 98, y = y, label = guards$fail[i], size = 2.05,
                      colour = if (last) "#1A1A1A" else C_NEUTRAL, hjust = 0)
    if (!last) {
      p <- p + annotate("segment", x = 66, xend = 94, y = y, yend = y,
                        colour = "#B9B9B9", linewidth = 0.3,
                        arrow = arrow(length = unit(0.045, "inches"), type = "closed"))
      p <- p + annotate("segment", x = 12, xend = 12, y = y - 3.3, yend = y - 5.7,
                        colour = "#6B6B6B", linewidth = 0.3,
                        arrow = arrow(length = unit(0.045, "inches"), type = "closed"))
    }
  }
  # The evidence stage feeds the chain, so the input path starts at that box and
  # doglegs into S0 rather than dropping out of an unrelated stage.
  p <- p +
    annotate("segment", x = 158, xend = 158, y = 76, yend = 69.2, colour = "#6B6B6B",
             linewidth = 0.3) +
    annotate("segment", x = 158, xend = 33, y = 69.2, yend = 69.2, colour = "#6B6B6B",
             linewidth = 0.3) +
    annotate("segment", x = 33, xend = 33, y = 69.2, yend = 65.6, colour = "#6B6B6B",
             linewidth = 0.3,
             arrow = arrow(length = unit(0.05, "inches"), type = "closed")) +
    annotate("text", x = 96, y = 71.4, label = "one unit at a time: claim + evidence set",
             size = 1.9, colour = C_FAINT, fontface = "italic") +
    annotate("text", x = 80, y = 92.5,
             label = "a guard must pass before the next is reached; a failure leaves the chain and returns a status with its reason",
             size = 1.95, colour = C_FAINT, fontface = "italic", hjust = 0.5)

  p + coord_cartesian(xlim = c(0, 180), ylim = c(2, 95), expand = FALSE) +
    theme_void(base_size = 6.5, base_family = "sans") +
    theme(plot.margin = margin(2, 2, 2, 2))
}

# ------------------------------------------------------------------ figure 2 --
figure2 <- function() {
  s <- fromJSON(file.path(OUT, "summary.json"))
  raw <- s$by_type_reason$DECLARATION_COMPLETE
  keys <- names(raw)
  n <- as.integer(unlist(raw))
  status <- sub("\\|.*", "", keys)
  reason <- sub(".*\\|", "", keys)

  owner <- c(UNDECLARED_CAPABILITY = "publisher", DECLARATION_SUBSET = "publisher",
             NO_DECLARATION = "publisher", CHANNEL_ABSENT = "package format",
             NO_DECLARING_CHANNEL = "schema", CODE_OPAQUE = "tooling",
             CODE_UNAVAILABLE = "collection", CODE_GONE = "collection",
             IDENTITY_AMBIGUOUS = "alignment", FULL_MATCH = "none")
  d <- data.frame(status = status, reason = reason, n = n, stringsAsFactors = FALSE)
  d$owner <- unname(owner[d$reason])
  d$owner[is.na(d$owner)] <- "none"
  d$owner[d$owner == "none"] <- "—"
  d$label <- sprintf("%s · %s", d$status, d$reason)
  d <- d[order(d$n), ]
  d$label <- factor(d$label, levels = d$label)
  total <- sum(d$n)
  d$pct <- 100 * d$n / total
  d$action <- ifelse(d$owner == "publisher", "publisher can act",
                     ifelse(d$owner == "none", "nothing to repair",
                            "not the publisher's to close"))

  ggplot(d, aes(y = label, x = pct)) +
    geom_col(aes(fill = action), width = 0.62) +
    geom_text(aes(label = sprintf("%.1f%%  (%s)  ·  %s", pct, format(n, big.mark = ","),
                                  owner)),
              hjust = 0, nudge_x = 0.45, size = 2.15, colour = C_NEUTRAL) +
    scale_fill_manual(values = c("publisher can act" = C_BEHAV,
                                 "not the publisher's to close" = C_SCHEMA,
                                 "nothing to repair" = "#C9C9C9"), name = NULL) +
    scale_x_continuous(expand = c(0, 0)) +
    scale_y_discrete(expand = c(0, 0.6)) +
    coord_cartesian(xlim = c(0, 43), clip = "off") +
    labs(title = "What the audit returns, and who can act on it",
         subtitle = sprintf("%s claims over %s aligned units; the right column is the repairer",
                            format(total, big.mark = ","), format(s$units, big.mark = ",")),
         x = "share of claims (%)", y = NULL) +
    theme_classic(base_size = 6.5, base_family = "sans") +
    theme(plot.title = element_text(size = 8.2, face = "bold", hjust = 0),
          plot.subtitle = element_text(size = 6.2, colour = C_NEUTRAL, hjust = 0,
                                       margin = margin(t = 2.6, b = 4)),
          plot.title.position = "plot",
          axis.line.y = element_blank(), axis.ticks.y = element_blank(),
          axis.text.y = element_text(size = 6.0),
          legend.position = "bottom", legend.direction = "horizontal",
          legend.margin = margin(-1, 0, 0, 0),
          legend.key.size = unit(3.0, "mm"),
          legend.text = element_text(size = 5.9),
          plot.margin = margin(2, 4, 8, 3))
}

# ------------------------------------------------------------------ figure 4 --
figure4 <- function() {
  a <- fromJSON(file.path(OUT, "ablation.json"))
  ps <- a$per_source
  d <- data.frame(source = names(ps), stringsAsFactors = FALSE)
  d$n <- vapply(ps, function(x) as.numeric(x$n), numeric(1))
  get <- function(x, k) { v <- x$dist[[k]]; if (is.null(v)) 0 else as.numeric(v) }
  d$SUPPORTED <- vapply(ps, get, numeric(1), "SUPPORTED")
  d$PARTIAL <- vapply(ps, get, numeric(1), "PARTIAL")
  d$UNSUPPORTED <- vapply(ps, get, numeric(1), "UNSUPPORTED")
  d$INSUFFICIENT <- vapply(ps, get, numeric(1), "INSUFFICIENT")
  d$UNRESOLVED <- vapply(ps, get, numeric(1), "UNRESOLVED")
  d$coverage <- 100 * d$n / a$n
  d <- d[order(-d$coverage), ]

  full <- data.frame(source = "full chain (all units)", n = as.numeric(a$n),
                     SUPPORTED = as.numeric(a$dist$full$SUPPORTED),
                     PARTIAL = as.numeric(a$dist$full$PARTIAL),
                     UNSUPPORTED = as.numeric(a$dist$full$UNSUPPORTED),
                     INSUFFICIENT = as.numeric(a$dist$full$INSUFFICIENT),
                     UNRESOLVED = as.numeric(a$dist$full$UNRESOLVED),
                     coverage = 100, stringsAsFactors = FALSE)
  d <- rbind(d, full)

  long <- do.call(rbind, lapply(c("SUPPORTED", "PARTIAL", "UNSUPPORTED",
                                  "INSUFFICIENT", "UNRESOLVED"), function(k) {
    data.frame(source = d$source, n = d$n, coverage = d$coverage,
               verdict = k, value = 100 * d[[k]] / d$n, stringsAsFactors = FALSE)
  }))
  long$source <- factor(long$source, levels = rev(d$source))
  long$verdict <- factor(long$verdict,
                         levels = c("SUPPORTED", "PARTIAL", "UNSUPPORTED",
                                    "INSUFFICIENT", "UNRESOLVED"))
  cols <- c(SUPPORTED = C_SCHEMA, PARTIAL = "#8FA8C8", UNSUPPORTED = C_BEHAV,
            INSUFFICIENT = "#DCDCDC", UNRESOLVED = "#EFEFEF")
  right <- unique(long[, c("source", "coverage", "n")])

  ggplot(long, aes(y = source, x = value, fill = verdict)) +
    geom_col(width = 0.62, position = position_stack(reverse = TRUE),
             colour = "white", linewidth = 0.15) +
    geom_text(data = right, aes(y = source, x = 101.5,
                                label = sprintf("%s units · %.0f%% of corpus",
                                                format(n, big.mark = ","), coverage)),
              inherit.aes = FALSE, hjust = 0, size = 2.0, colour = C_FAINT) +
    scale_fill_manual(values = cols, breaks = c("SUPPORTED", "PARTIAL",
                                                "UNSUPPORTED", "INSUFFICIENT"),
                      name = NULL) +
    scale_x_continuous(limits = c(0, 148), breaks = c(0, 25, 50, 75, 100),
                       expand = c(0, 0)) +
    scale_y_discrete(expand = c(0, 0.55)) +
    labs(title = "No single registry is a vantage point",
         subtitle = "verdict mix within each source's own listings, against the full chain on all units",
         x = "share of that source's listings (%)", y = NULL) +
    theme_classic(base_size = 6.5, base_family = "sans") +
    theme(plot.title = element_text(size = 8.2, face = "bold", hjust = 0),
          plot.subtitle = element_text(size = 6.2, colour = C_NEUTRAL, hjust = 0,
                                       margin = margin(t = 2.6, b = 4)),
          plot.title.position = "plot",
          axis.line.y = element_blank(), axis.ticks.y = element_blank(),
          axis.text.y = element_text(size = 6.2),
          axis.title.x = element_text(size = 6.6),
          legend.position = "bottom", legend.direction = "horizontal",
          legend.margin = margin(-1, 0, 0, 0),
          legend.key.size = unit(3.2, "mm"),
          legend.text = element_text(size = 5.9),
          plot.margin = margin(2, 6, 8, 3))
}

cat("figure 1 -- the audit pipeline\n")
fig1 <- figure1()
save_pub_r(fig1, "fig1_pipeline", 160, 73, multi_panel = FALSE)
cat("figure 2 -- verdicts and repairers\n")
fig2 <- figure2()
save_pub_r(fig2, "fig2_verdicts", 160, 97, multi_panel = FALSE)
cat("figure 4 -- single-registry coverage\n")
fig4 <- figure4()
save_pub_r(fig4, "fig4_coverage", 160, 114, multi_panel = FALSE)

cat("figure 3 -- omission decomposition\n")
fig3 <- figure3()

cat("figure 5 -- precision confirmation\n")
fig5 <- figure5()
