## 30_security.R -- Fig. 7 (detected surfaces / declaration gap), Fig. 8 (attribution),
##                  Fig. 9 (single-registry blind spot), Fig. 10 (silent drift)
source("paper/cs_paper/R/00_theme.R")
D <- "paper/cs_paper/data"

## ---------------- Fig. 7 : detected surfaces + declaration gap ---------------
dg <- read.csv(file.path(D, "d_danger.csv"), stringsAsFactors = FALSE)
dg$category <- factor(dg$category, levels = dg$category[order(dg$repos)])
pa <- ggplot(dg, aes(repos / 1000, category)) +
  geom_col(fill = PAL("vermillion"), colour = "black", linewidth = 0.25, width = 0.62) +
  geom_text(aes(label = sprintf("%s  (%.1f%%)", format(repos, big.mark = ","),
                                round(100 * repos / 86983, 1))),
            hjust = -0.05, size = 1.85, colour = "grey15") +
  scale_x_continuous(limits = c(0, 82), labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0.02))) +
  labs(x = "Cloned repositories with a static hit (thousands)", y = NULL) + theme_cs()

## Panel (b) separates the three classes that a manifest can declare from the six
## that have no declaration channel in the format, so that a structural omission is
## never read as a publisher's choice.
og <- read.csv(file.path(D, "d_overgrant2.csv"), stringsAsFactors = FALSE)
NM <- c(env_access = "Environment access", network_egress = "Network egress",
        unsafe_deserialize = "Unsafe deserialization", subprocess = "Subprocess spawn",
        arbitrary_code = "Arbitrary code eval", credential_paths = "Credential paths",
        fs_destructive = "Destructive FS write", db_access = "Database access",
        shell_true = "Shell execution")
NM2 <- c("Environment access" = "env_access", "Network egress" = "network_egress",
         "Unsafe deserialization" = "unsafe_deserialize", "Subprocess spawn" = "subprocess",
         "Arbitrary code evaluation" = "arbitrary_code",
         "Credential paths" = "credential_paths",
         "Destructive filesystem write" = "fs_destructive",
         "Database access" = "db_access", "Shell execution" = "shell_true")
og$key <- NM2[og$category]
og$lab <- factor(NM[og$key], levels = NM[og$key][order(og$declarable, og$units)])
og$fillcat <- factor(ifelse(og$declarable == "yes",
                            "Undeclared, field exists", "Undeclared, no field exists"),
                     levels = c("Declared", "Undeclared, field exists",
                                "Undeclared, no field exists"))
fill_levels <- levels(og$fillcat)
og$chan <- factor(ifelse(og$declarable == "yes",
                         "Declaration field exists in the manifest format",
                         "No declaration field in the manifest format"),
                  levels = c("Declaration field exists in the manifest format",
                             "No declaration field in the manifest format"))
## The six no-field classes are all fully undeclared. State that once in the
## group label and keep only the count beside those bars; this avoids repeating
## the same percentage six times while preserving the statistic.
og$txt <- ifelse(
  og$declarable == "yes",
  sprintf("%s / %s (%.1f%%)", format(og$undeclared, big.mark = ","),
          format(og$mp_units, big.mark = ","), og$share_pct),
  format(og$undeclared, big.mark = ",")
)
og$label_inside <- (og$declarable == "yes" & og$undeclared >= 6000) |
  (og$declarable == "no" & og$undeclared >= 2500)
pb <- ggplot(og, aes(mp_units, lab)) +
  ## The grey remainder is the declared share. It is mapped rather than fixed so that it
  ## appears in the legend beside the two undeclared categories.
  geom_col(aes(fill = factor("Declared", levels = fill_levels)),
           colour = "black", linewidth = 0.25, width = 0.62) +
  geom_col(aes(x = undeclared, fill = fillcat), colour = "black", linewidth = 0.25, width = 0.62) +
  geom_text(data = subset(og, label_inside),
            aes(x = undeclared - 120, label = txt),
            hjust = 1.03, size = 2.0, lineheight = 0.88, fontface = "plain",
            colour = "grey20", show.legend = FALSE) +
  geom_text(data = subset(og, !label_inside),
            aes(x = mp_units + 150, label = txt),
            hjust = 0, size = 2.0, lineheight = 0.88, fontface = "plain",
            colour = "grey35", show.legend = FALSE) +
  facet_grid(chan ~ ., scales = "free_y", space = "free_y") +
  scale_fill_manual(values = c("grey86", unname(PAL("vermillion", "sky"))),
                    drop = FALSE, name = NULL) +
  scale_x_continuous(limits = c(0, 13000), breaks = seq(0, 12000, 4000),
                     labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0.02))) +
  scale_y_discrete(expand = expansion(add = c(0.55, 0.55))) +
  scale_colour_identity() +
  labs(x = "Manifest-present units with a detected class", y = NULL) +
  theme_cs() +
  theme(legend.position = "bottom", legend.margin = margin(0, 0, 0, 0),
        legend.key.size = unit(6, "pt"),
        legend.spacing.x = unit(1.5, "pt"),
        legend.text = element_text(size = BASE_PT - 0.5),
        strip.background = element_blank(),
        strip.text.y = element_blank(),
        panel.spacing.y = unit(3, "pt"))
p7 <- ((pa + pb + plot_layout(widths = c(0.50, 0.50))) +
       plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")")) &
  theme(plot.tag = element_text(size = BASE_PT + 1.5, face = "bold"))
save_fig(p7, "fig08_danger", W2, MM(74))

## ---------------- Fig. 8 : disclosure-gap attribution -----------------------
bl <- read.csv(file.path(D, "d_blame2.csv"), stringsAsFactors = FALSE)
bl$rate <- 100 * bl$numerator / bl$with_code
DISP <- c(smithery = "Smithery", modelscope = "ModelScope", cursor = "Cursor", glama = "Glama",
         mcpso = "mcp.so", official = "Official", aibase = "AIBase")
bl$name <- factor(DISP[bl$registry], levels = DISP[bl$registry[order(bl$rate)]])
p8 <- ggplot(bl, aes(rate, name)) +
  geom_col(fill = PAL("purple"), colour = "black", linewidth = 0.25, width = 0.6) +
  geom_text(aes(label = sprintf("%.1f%%", rate)), hjust = -0.06, size = 2.0, colour = "grey10") +
  geom_text(aes(label = sprintf("%s / %s", format(numerator, big.mark = ","),
                                format(with_code, big.mark = ","))),
            hjust = 1.12, size = 1.8, colour = "white") +
  geom_point(aes(x = 100 * cross_disagreement / with_code), shape = 1, size = 1.7,
             stroke = 0.5, colour = PAL("vermillion")) +
  annotate("text", x = 2.5, y = 0.46,
           label = "0 units for every registry: the cross-source disagreement component",
           hjust = 0, vjust = 0, size = 1.72, colour = PAL("vermillion")) +
  scale_x_continuous(limits = c(0, 124), breaks = seq(0, 100, 25),
                     labels = function(x) paste0(x, "%"), expand = expansion(mult = c(0.015, 0.02))) +
  scale_y_discrete(expand = expansion(add = c(0.75, 0.6))) +
  labs(x = "Units whose code exceeds the collective declaration,\nover units with code evidence",
       y = NULL) + theme_cs()
save_fig(p8, "fig09_blame", W1, MM(62))

## ---------------- Fig. 9 : single-registry blind spot -----------------------
st <- read.csv(file.path(D, "d_singletable.csv"), stringsAsFactors = FALSE)
## The miss rate shares the denominator of code-flagged units, not the row sum.
st$miss <- 100 * st$missed / (st$covered + st$missed)
st$name <- factor(DISP[st$registry], levels = DISP[st$registry[order(st$covered)]])
pa9 <- ggplot(st, aes(covered / 1000, name)) +
  geom_col(fill = PAL("green"), colour = "black", linewidth = 0.25, width = 0.6) +
  geom_text(aes(label = sprintf("%s  (misses %.1f%%)", format(covered, big.mark = ","),
                                round(miss, 1))),
            hjust = -0.05, size = 1.85, colour = "grey15") +
  scale_x_continuous(limits = c(0, 105), labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0.02))) +
  labs(x = "Code-flagged repositories listed by this registry (thousands)", y = NULL) + theme_cs()

vk <- read.csv(file.path(D, "d_visibility_k.csv"), stringsAsFactors = FALSE)
vk$lab <- factor(c("1 registry", "2 registries", "\u2265 3 registries"),
                 levels = c("1 registry", "2 registries", "\u2265 3 registries"))
pb9 <- ggplot(vk, aes(lab, units / 1000)) +
  geom_col(fill = PAL("sky"), colour = "black", linewidth = 0.25, width = 0.58) +
  geom_text(aes(label = sprintf("%s\n(%.1f%%)", format(units, big.mark = ","), pct)),
            vjust = -0.35, size = 1.9, lineheight = 0.92, colour = "grey10") +
  scale_y_continuous(limits = c(0, 75), labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0.05))) +
  labs(x = "Registries listing the repository", y = "Code-flagged repositories (thousands)") + theme_cs()
p9 <- pa9 + pb9 + plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")") &
  theme(plot.tag = element_text(size = BASE_PT + 1.5, face = "bold"))
save_fig(p9, "fig10_blindspot", W2, MM(66))

## ---------------- Fig. 10 : silent implementation drift ---------------------
dr <- read.csv(file.path(D, "d_drift_out.csv"), stringsAsFactors = FALSE)
lv <- c("Advanced", "Unchanged", "did not resolve at observation", "Timeout")
dr$outcome <- factor(dr$outcome, levels = lv)
lab <- c(Advanced = "5,069\n(35.1% of resolvable)", Unchanged = "9,388\n(64.9% of resolvable)",
         "did not resolve at observation" = "338\n(2.3%)", Timeout = "2\n(<0.1%)")
dr$txt <- lab[as.character(dr$outcome)]
pa10 <- ggplot(dr, aes(outcome, units / 1000)) +
  geom_col(aes(fill = outcome), colour = "black", linewidth = 0.25, width = 0.58) +
  geom_text(aes(label = txt), vjust = -0.35, size = 1.95, lineheight = 0.92, colour = "grey10") +
  annotate("text", x = 2.65, y = 10.4, hjust = 0, vjust = 1, size = 1.8, colour = "grey20",
           label = "Advanced or did not resolve at observation:\n5,407 of 14,797 (36.5%)") +
  scale_fill_manual(values = c(PAL("vermillion"), PAL("grey"), PAL("purple"), PAL("sky")),
                    guide = "none") +
  scale_y_continuous(limits = c(0, 12.6), labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0.05))) +
  labs(x = NULL, y = "Declared-frozen units (thousands)") + theme_cs() +
  theme(axis.text.x = element_text(size = BASE_PT - 0.7))

rc <- read.csv(file.path(D, "d_reclone_cases.csv"), stringsAsFactors = FALSE)
rc <- rc[rc$status == "ahead", ]
rc$ahead_by <- as.numeric(rc$ahead_by)
## Bin counts are computed explicitly so that no bar is silently dropped by the
## y-scale limits (a plain geom_histogram + limits would discard the tallest bin).
logv   <- log10(rc$ahead_by)
edges  <- seq(min(logv), max(logv), length.out = 15)
brks   <- cut(logv, breaks = edges, include.lowest = TRUE, labels = FALSE)
hb     <- data.frame(mid = (edges[-1] + edges[-length(edges)]) / 2,
                     n   = as.integer(table(factor(brks, levels = seq_along(edges[-1])))))
hb$lo  <- 10^(hb$mid - diff(edges)[1] / 2); hb$hi <- 10^(hb$mid + diff(edges)[1] / 2)
pb10 <- ggplot(hb, aes(xmin = lo, xmax = hi, ymin = 0, ymax = n)) +
  geom_rect(fill = PAL("vermillion"), colour = "black", linewidth = 0.25) +
  geom_vline(xintercept = median(rc$ahead_by), linetype = "dashed", linewidth = 0.35, colour = "grey25") +
  # Annotation moved right of the tallest bar at the proof stage so the label
  # clears the first bar. The shipped PNG raster was patched in place to match.
  annotate("text", x = median(rc$ahead_by) * 1.6, y = max(hb$n) * 0.86,
           label = sprintf("median = %.1f", median(rc$ahead_by)),
           size = 1.9, colour = "grey20", hjust = -0.05, vjust = 0.5) +
  scale_x_log10(limits = c(0.8, 700), breaks = c(1, 3, 10, 30, 100, 300),
                labels = c("1", "3", "10", "30", "100", "300")) +
  scale_y_continuous(limits = c(0, max(hb$n) * 1.14), breaks = seq(0, max(hb$n) + 1, by = 2)) +
  labs(x = "Commits advanced since the recorded revision (log scale)",
       y = "Number of cases") + theme_cs()
p10 <- pa10 + pb10 + plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")") &
  theme(plot.tag = element_text(size = BASE_PT + 1.5, face = "bold"))
save_fig(p10, "fig11_drift", W2, MM(66))
