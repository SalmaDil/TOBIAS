#!/usr/bin/env python

"""
Script to compare two sets of motifs with each other. Generating similarity matrix and a clustered heatmap.
If only one motif file is given, it will be compared with itself.

@author: René Wiegandt, Anastasiia Petrova
@contact: rene.wiegandt (at) mpi-bn.mpg.de, anastasiia.petrova (at) mpi-bn.mpg.de
@license: MIT

"""

import argparse
import numpy as np
import seaborn as sns
import pandas as pd
import datetime
import os
import yaml
import re
import math
import itertools
import warnings
from Bio import motifs
from matplotlib import pyplot as plt
from gimmemotifs.motif import Motif,read_motifs
from gimmemotifs.comparison import MotifComparer
from scipy.cluster.hierarchy import dendrogram, linkage, fcluster
import scipy.spatial.distance as ssd
from collections import defaultdict

from tobias.utils.utilities import *
from tobias.utils.logger import *
from tobias.utils.motifs import *

#--------------------------------------------------------------------------------------------------------#
def add_motifclust_arguments(parser):
    """Parsing arguments using argparse

    Returns
    -------
    ArgumentParser
        an object containing all parameters given.
    """

    parser.formatter_class = lambda prog: argparse.RawDescriptionHelpFormatter(prog, max_help_position=40, width=90)
    description = "Cluster motifs based on similarity and create one consensus motif per cluster.\n\n"
    description += "Usage:\nTOBIAS ClusterMotifs --motifs <motifs.jaspar>\n\n"
    parser.description = format_help_description("ClusterMotifs", description) 

    parser._action_groups.pop() #pop -h

    required = parser.add_argument_group('Required arguments')
    required.add_argument("-m", "--motifs", required=True, help="One or more motif files to compare and cluster", nargs="*", metavar="")
    
    optional = parser.add_argument_group('Optional arguments')
    optional.add_argument("-t", "--threshold", help="Clustering threshold (Default = 0.5)", type=float, default=0.5)  
    optional.add_argument('--dist_method', help="Method for calculating similarity between motifs (default: pcc)", choices=["pcc", "seqcor", "ed", "distance", "wic", "chisq", "akl", "sdd"], default="pcc")
    optional.add_argument('--clust_method', help="Method for clustering (See: https://docs.scipy.org/doc/scipy/reference/generated/scipy.cluster.hierarchy.linkage.html)", default="average", choices=["single","complete","average","weighted","centroid","median","ward"])
    optional.add_argument("-a", "--cons_format", choices= ['transfac', 'meme', 'pwm'], help="Format of consensus motif file [‘transfac’, ‘meme’, ‘pwm’] (Default: pwm)", default="pwm")
    optional.add_argument("-p", "--prefix", help="Output prefix (Default: ‘motif_comparison’)", default="motif_comparison")
    optional.add_argument("-o", "--outdir", help="Output directory (Default: ‘./ClusterMotifs‘)", default="ClusterMotifs")
    optional = add_logger_args(optional)

    visualisation = parser.add_argument_group('Visualisation arguments')
    visualisation.add_argument("-e", "--type", dest="type", choices= ['png', 'pdf', 'jpg'], help="Plot file type [png, pdf, jpg] (Default: pdf)", default="pdf")
    visualisation.add_argument("-x", "--width", dest="width", help="Width of Heatmap (Default: autoscaling)", type=int)
    visualisation.add_argument("-y", "--height", dest="height", help="Height of Heatmap (Default: autoscaling)", type=int)
    visualisation.add_argument("-d", "--dpi", dest="dpi", help="Dpi for plots (Default: 100)", type=int, default=100)
    #visualisation.add_argument("-ncc", "--no_col_clust", dest="ncc", help="No column clustering", action="store_true")
    #visualisation.add_argument("-nrc", "--no_row_clust", dest="nrc", help="No row clustering", action="store_true")
    visualisation.add_argument("-c", "--color_palette", dest="color", help="Color palette (All possible paletts: https://python-graph-gallery.com/197-available-color-palettes-with-matplotlib/. Add '_r' to reverse palette.)", default="YlOrRd_r")

    return parser


#--------------------------------------------------------------------------------------------------------#
def get_motifs(path, format):
    """Read motif file via gimmemotifs or biopython. 
    If motif is read via biopython the bio.motif instace gets converted to an gimmemotif instance.

    Parameter:
    ----------
    path : String
        a path to a motif file
    format : String
        format of motif file

    Returns:
    --------
    list
        a list of motif instances
    """

    gimme_formats = ["pwm", "transfac", "xxmotif", "align"]

    # Read motif via gimmemotifs if format is supported
    if (format in gimme_formats):
        gimme_motif_list = read_motifs(infile = path, fmt = format, as_dict = False)

        """
        elif format == "meme":
            motiflist = MotifList().from_file(path)

            gimme_motif_list = []
            for motif in motiflist:
                gimme_motif = Motif(motif.counts)
                gimme_motif.id = motif.id
                gimme_motif_list.append(gimme_motif)
        """

    else: 

        #ugly hack for reading minimal meme
        if format == "meme":
            format = "minimal"

        # Otherwise Bio.python is used
        with open(path) as f:
            bio_motif_list = motifs.parse(f, format)

        # Get full motif name if format is minimal
        if format == "minimal":
            name_list = get_minimal_names(path)

        # Converting Bio.motif instance to gimmemotif motif instance
        gimme_motif_list = list() # list of all motifs
        for i, bio_motif in enumerate(bio_motif_list):
            motif_rows = list()

            for pos_id in range(bio_motif.length-1):
                row = list() # each row represents one motif index ( A C G T )
                for letter in range(4):
                    row.append(bio_motif.counts[letter][pos_id])
                motif_rows.append(row)

            gimme_motif = Motif(motif_rows) # generate gimmemotif motif instance
            
            # Add motif name
            if format == "minimal":
                gimme_motif.id = name_list[i]
            else:
                gimme_motif.id = bio_motif.name
            gimme_motif_list.append(gimme_motif)


    return gimme_motif_list


#--------------------------------------------------------------------------------------------------------#
def get_minimal_names(path):
    """Get list of full motif names from minimal meme files

    Parameter:
    ----------
    path : String
        a path to a motif file

    Returns:
    --------
    List
        a list containing all motif names
    """

    motif_names = list()
    with open(path) as f:
        for line in f.readlines():
            if line.startswith("MOTIF"): # get motif header
                motif_names.append(re.sub("MOTIF\s+", "", line).rstrip())
    return motif_names


#--------------------------------------------------------------------------------------------------------#
def generate_similarity_matrix(score_dict):
    """Generate a similarity matrix from the output of get_all_scores()

    Parameter:
    ----------
    score_dict : dict
        a dictionary of dictionarys containing a list of similarity scores

    Returns:
    --------
    DataFrame
        a DataFrame (Pandas) with motif 1 a columns and motif 2 as rows
    """

    m1_keys = list(score_dict.keys())
    m2_keys = list(score_dict.values())[0].keys()   #should be similar to m1_keys

    m1_labels = [s.replace('\t', ' ') for s in m1_keys] # replacing tabs with whitespace
    m2_labels = [s.replace('\t', ' ') for s in m2_keys]
    
    #Make sure similarity dict is symmetrical:
    similarity_dict = {m:{} for m in m1_labels}  #initialize dict
    for i, m1 in enumerate(m1_keys):
        for j, m2 in enumerate(m2_keys):    
            score = round(1 - np.mean([score_dict[m1][m2][0], score_dict[m2][m1][0]]), 3)
            
            similarity_dict[m1_labels[i]][m2_labels[j]] = score
            similarity_dict[m2_labels[j]][m1_labels[i]] = score

    #Format similarity dict to dataframe
    similarity_dict_format = {m1: [similarity_dict[m1][m2] for m2 in m2_labels] for m1 in m1_labels}
    dataframe = pd.DataFrame(similarity_dict_format, index = m2_labels).replace(-0, 0)

    return dataframe


#--------------------------------------------------------------------------------------------------------#
def subset_matrix(matrix, name_list_1, name_list_2):
    """Subsetting matrix into two symetric matrices given two label lists

    Parameter:
    ----------
    matrix :  Pandas DataFrame
        Full similarity matrix
    name_list_1 : list
        list of names
    name_list_2 : list
        list of names

    Returns:
    --------
    list:
        list with subsets of matrix
    """

    sim_matrix_1 = matrix[name_list_1].loc[name_list_1]
    sim_matrix_2 = matrix[name_list_2].loc[name_list_2]
    sim_matrix = matrix[name_list_1].loc[name_list_2]
    return sim_matrix_1, sim_matrix_2, sim_matrix


#--------------------------------------------------------------------------------------------------------#
def get_dissimilar_motifs(matrix, threshold):
    """Get motifs that are not similar to any of the motifs compared to them.

    Parameter:
    ----------
    matrix: DataTable
        a DataTable contaning the similarity scores of the motifs.
    threshold : float
        threshold for similarity score
    
    Return:
    ---------
    dissimilar_motifs : list
        list of motifs that are not similar to to any of the motifs compared to them.
    """

    columns = matrix.shape[1]
    dissimilar_motifs = list()
    for col in range(columns-1):
        col_vec = matrix.iloc[:,col] > threshold
        if(col_vec.eq(True).all()):
            dissimilar_motifs.append(matrix.columns[col])
    return dissimilar_motifs


#--------------------------------------------------------------------------------------------------------#
def get_gc_content(motifs):
    """Get the GC content and the length for a list of motifs

    Parameter:
    ----------
    motifs : list
        A list of gimmemotifs motif objects.
    
    Return:
    -------
    motif_stats : dict
        A dictionary containing the motif name as key and
        a list with GC content and motif length as value.
    """

    motif_stats = dict()
    for m in motifs:
        # Remove first row
        m_woh = re.sub('^>.*\n','',m.to_pwm())
        m_pwm = m_woh.split()
        motif_length = len(m)
        gc_sum = 0
        # Get sum of G and C ratios
        for x in range(0, len(m_pwm)-1, 4):
            gc_sum += float(m_pwm[x+1]) + float(m_pwm[x+2])

        # Calc GC ratio of the motif
        gc_content = gc_sum/motif_length
        motif_name = m.id.replace('\t', ' ')
        motif_stats[motif_name] = [round(gc_content,4), motif_length]

    return motif_stats


#--------------------------------------------------------------------------------------------------------#
def write_motif_stats(stats, out_file):
    """Write stats to file

    Parameter:
    ----------
    stats: dict
        dictionary with motif name as key and list with stats as value
    out_file: string
        path and name of out-file
    """

    with open(out_file, "w") as f: 
        f.write("Motif\tGC content\tMotif length\n")
        for motif_name,stats in stats.items():
            stats_string = "\t".join(map(str, stats))
            f.write("{}\t{}\n".format(motif_name, stats_string))


#--------------------------------------------------------------------------------------------------------#
def cluster_motifs(similarity_matrix, threshold, method, subset_1=None, subset_2=None):
    """Clusters motif similarity matrix hierarchically

    Parameter:
    ----------
    similarity_matrix : DataTable
        a DataTable contaning the similarity score
    threshold : float
        clustering threshold
    method : string
        clustering method used by scipy.cluster.hierarchy.linkage
    
    Returns:
    --------
    List : ndarray, dict
        The hierarchical clustering of rows and cols encoded as a linkage matrix.
        A dictionary containing named clusters for rows and cols.
    """

    # Clustering
    vector = ssd.squareform(similarity_matrix.to_numpy())
    linkage_mat = linkage(vector, method=method)

    # Flatten cluster
    labels = fcluster(linkage_mat, threshold, criterion="distance")

    # Extract cluster
    cluster = get_cluster(labels, similarity_matrix.index.values)

    return linkage_mat, cluster


#--------------------------------------------------------------------------------------------------------#
def get_cluster(labels, label_names):
    """Map clusternumber to label names

    Parameter:
    ----------
    labels : ndarray
        An array of length n. T[i] is the flat cluster number.
    label_names : list
        List of names
    
    Return:
    -------
    cluster : dict
        A dictionary containing named clusters.
    """

    cluster = defaultdict(list)
    for i in range(len(labels)):
        cluster["Cluster_" + str(labels[i])].append(label_names[i])
    return cluster


#--------------------------------------------------------------------------------------------------------#
def scaling(axis_len):
    """Scaling of plot figure size

    Parameter:
    ----------
    axis_len : int
        Number of rows or columns

    Return:
    ----------
    scale : float
        scale for axis
    """

    return 0.5/math.log10(axis_len)


#--------------------------------------------------------------------------------------------------------#
def write_yaml(clusters, yml_out):
    """Writes cluster dict to yaml

    Parameter:
    ----------
    clusters : dict
        dictionary containing lists as values.
    yml_out : string
        path for outfile
    """

    with open(yml_out, 'w') as outfile:
        yaml.dump(dict(clusters), outfile, default_flow_style=False)


#--------------------------------------------------------------------------------------------------------#
def plot_dendrogram(label, linkage, font_size, out, title, threshold, y, dpi, t):
    """Plot dendrogram with highlighted threshold 

    Parameter:
    ----------
    label : list
        List of labels
    linkage : ndarray
        The hierarchical clustering of rows or cols encoded as a linkage matrix.
    font_size : int
        font size
    out : String
        Output path
    title : String
        Plot title
    threshold : float
        dendrogram cluster threshold
    y : int
        yaxis size of plot
    dpi : int
        dpi of plot
    t : string
        type of plot file
    """

    plt.figure(figsize=(20, y))
    plt.title(title, fontsize=20)
    plt.axvline(x=threshold, color="red")
    dendrogram(linkage, color_threshold=threshold, labels=label, leaf_font_size=font_size, orientation="right")
    try:
        plt.savefig(out + "." + t, dpi=dpi)
    except ValueError as e:
        print("Skipped ploting of Heatmap.")
        print("Error: " + str(e))


#--------------------------------------------------------------------------------------------------------#
def generate_consensus_motifs(motif_list, cluster, similarity_dict):
    """Generates a consensus motif for each cluster

    Parameter:
    ----------
    motif_list : list
        list of all motif instances
    cluster : dict
        A dictionary containing named clusters.
    similarity_dict : dict
        Output from gimmemotif.get_all_scores

    Returns:
    --------
        dict of consensus motifs as gimmemotif motif instance
    """

    consensus_motifs = dict()
    for cluster, value in cluster.items():
        cluster_motifs = list()
        # Get motif instances
        for m in motif_list:
            if m.id in value:
                cluster_motifs.append(m)
        consensus_motifs[cluster] = generate_consensus_motif(cluster_motifs, similarity_dict)
    return consensus_motifs


#--------------------------------------------------------------------------------------------------------#
def generate_consensus_motif(motifs, similarity_dict):
    """Recursive algorithm to generate consensus motif from multiple motifs

    Parameter:
    ----------
    motifs : list
        list of gimmemotif motif instances
    similarity_dict : dict
        Output from gimmemotifs.get_all_scores

    Returns:
    --------
    gimmemotifs motif instance
        consensus motif of all motifs in motifs list
    """

    mc = MotifComparer()

    if len(motifs) == 1:
        return motifs[0]

    # score cannot be lower that 0 so max score is initialized with 0
    max_score = 0
    for motif_1, motif_2  in itertools.combinations(motifs, 2):
        score = similarity_dict[motif_1.id][motif_2.id]
        if score[0] >= max_score:
            max_score = score[0]
            merge_1 = motif_1
            merge_2 = motif_2

    # generate average motif
    average_motif = merge_1.average_motifs(merge_2, pos=score[1], orientation=score[2])
    average_motif.id = merge_1.id + "_" + merge_2.id

    # alter list 
    motifs.append(average_motif)
    motifs.remove(merge_1)
    motifs.remove(merge_2)

    sim_dict = mc.get_all_scores(motifs, motifs, match = "total", metric = "pcc", combine = "mean" )

    cons_motif = generate_consensus_motif(motifs, sim_dict)
    return cons_motif


#--------------------------------------------------------------------------------------------------------#
def save_motif_image(consensus_motif, prefix, ext, out):
    """Save motif to image file

    Paramater:
    ----------
    consensus_motif : gimmemotif motif instance
        The motif to be plotted
    prefix : string
        Prefix for image file
    ext : string
        File format
    out : string
        Output path
    """

    if ext == "jpg":
        ext = "png"
        warnings.warn("The 'jpg' format is not supported for motif image. Type is set tp 'png'")
    consensus_motif.to_img(os.path.join(out, prefix + "_consensus." + ext))


#--------------------------------------------------------------------------------------------------------#
def convert_motif(consensus_motif, f):
    """ Convert motif to given format

    Parameter:
    ----------
    consensus_motif : gimmemotif motif instance
        The motif to be written to file
    out : string
        Output string without file ending
    f : string
        Format of motif file

    Returns:
    --------
    String
        Motif in given format
    """

    if f == "meme":
        motif = consensus_motif.to_meme()
    elif f == "pwm":
        motif = consensus_motif.to_pwm()
    elif f == "transfac":
        motif = consensus_motif.to_transfac()
    else:
        raise ValueError("Format " + f + " is not supported")
    return motif


#--------------------------------------------------------------------------------------------------------#
def consensus_motif_out_wrapper(consensus_motifs, out_prefix, img_out, cons_format, name, typ, file_name = None):
    """ Wrapper for consensus motif output

    Parameter:
    ----------
    consensus_motifs : dict
        dictionary a consensur motif for each cluster
    out_prefix : string
        Output path + prefix
    img_out : string
        Path for images
    cons_format : string
        Format for motif file
    name : string
        Prefix for motif images
    file_name : string
        File name of clustered motifs
    typ : string
        File format of image
    """

    if file_name is not None:
        img_prefix = name + "_" + file_name
        cons_out_path = out_prefix + "_" + file_name + "_consensus_motifs." + cons_format
    else:
        img_prefix = name 
        cons_out_path = out_prefix + "_consensus_motifs." + cons_format

    cons_out = open(cons_out_path, 'w+')

    for cluster, consensus_motif in consensus_motifs.items():
        consensus_motif.id = cluster + "_" + consensus_motif.id

        # Save motif as image
        save_motif_image(consensus_motif, img_prefix + "_" + cluster, typ, img_out)

        # Save motifs to file
        motif_string = convert_motif(consensus_motif, cons_format)
        cons_out.write(motif_string + "\n\n")


#--------------------------------------------------------------------------------------------------------#
def plot_heatmap(similarity_matrix, out, x, y, col_linkage, row_linkage, dpi, x_name, y_name, color, col_clust, row_clust, zscore):
    """Plots clustered similarity matrix as a heatmap

    Parameter:
    ----------
    similarity_matrix :  DataTable
        a DataTable contaning the similarity score
    out : string
        Prefix to output file
    x : int
        width for heatmap plot
    y : int
        height for heatmap plot
    col_linkage : ndarray
        The hierarchical clustering of cols encoded as a linkage matrix.
    row_linkage : ndarray
        The hierarchical clustering of rows encoded as a linkage matrix.
    dpi : int
        dpi of plot
    x_name : string
        label for x-axis
    y_name : string
        label for y-axis
    color : string
        colorpalette
    col_clust : boolean
        if True, cluster the columns
    row_clust : boolean
        if True, cluster the rows
    """

    vmin, vmax = None, None
    if zscore == "row":
        zs = 0
    elif zscore == "col":
        zs = 1
    else:
        zs = None
        vmin, vmax = 0, 1

    try:
        plot = sns.clustermap(similarity_matrix,
            row_linkage=row_linkage,
            col_linkage=col_linkage,
            col_cluster= not col_clust,
            row_cluster= not row_clust,
            z_score=zs,
            cmap=color,
            vmin=vmin, vmax=vmax,
            xticklabels=similarity_matrix.columns,
            yticklabels=similarity_matrix.index.values,
            figsize=(x,y))
        plot.ax_heatmap.set_xlabel(x_name)
        plot.ax_heatmap.set_ylabel(y_name)
        plot.savefig(out, bbox_inches='tight', dpi=dpi)
    except ValueError as e:
        print("Skipped ploting of Heatmap.")
        print("Error: " + str(e))


#--------------------------------------------------------------------------------------------------------#
def run_motifclust(args):

    ###### Check input arguments ######
    check_required(args, ["motifs"])           #Check input arguments
    check_files([args.motifs])   #Check if files exist
    make_directory(args.outdir)
    out_prefix = os.path.join(args.outdir, args.prefix)

    ###### Create logger and write argument overview ######
    logger = TobiasLogger("ClusterMotifs", args.verbosity)
    logger.begin()
    
    parser = add_motifclust_arguments(argparse.ArgumentParser())
    logger.arguments_overview(parser, args)
    #logger.output_files([])

    out_prefix = os.path.join(args.outdir, args.prefix)

    #---------------------------------------- Reading motifs from file(s) -----------------------------------#

    logger.info("Handling input file(s)")

    motif_list = [] #list containing gimmemotifs-motif-objects
    motif_dict = {} #dictionary containing separate motif lists per file
    for f in args.motifs:
        logger.debug("Reading {0}".format(f))

        #Establish file format
        motif_format = get_motif_format(open(f).read())

        #Read motifs to internal structure
        sub_motif_list = get_motifs(f, motif_format)
        m_names = list(m.id for m in sub_motif_list)
        logger.stats("- Read {0} motifs from {1} (format: {2})".format(len(sub_motif_list), f, motif_format))

        motif_list.extend(sub_motif_list)
        motif_dict[f] = sub_motif_list


    #---------------------------------------- Generating similarity matrix ----------------------------------#
    
    logger.info("Generating similarity matrix")

    mc = MotifComparer()    #gimmemotifs class to compare motif lists

    # Pairwise comparison of motifs
    score_dict = mc.get_all_scores(motif_list, motif_list, match = "total", metric = args.dist_method, combine = "mean")   #metric can be: seqcor, pcc, ed, distance, wic, chisq, akl or ssd

    # Generating similarity matrix
    similarity_matrix = generate_similarity_matrix(score_dict)

    # Save similarity matrix to file
    matrix_out = out_prefix + "_matrix.txt"
    logger.info("- Saving similarity matrix to the file: " + str(matrix_out))
    similarity_matrix.to_csv(matrix_out, sep = '\t')


    #---------------------------------------- Motif stats ---------------------------------------------------#

    logger.info("Making matrix statistics about dissimilar motifs and GC-content")
    
    #Stats for all motifs
    full_motifs_out = out_prefix + "_stats_motifs.txt"
    motifs_stats = get_gc_content(motif_list)
    write_motif_stats(motifs_stats, full_motifs_out)

    # Get motif stats only for dissimilar motifs
    dissimilar_motifs_out = out_prefix + "_dissimilar_motifs.txt"
    dissimilar_motifs = get_dissimilar_motifs(similarity_matrix, args.threshold)
    stats_dissimilar_motifs = dict((k, motifs_stats[k]) for k in dissimilar_motifs)
    write_motif_stats(stats_dissimilar_motifs, dissimilar_motifs_out)
    

    #---------------------------------------- Motif clustering ----------------------------------------------#
    
    logger.info("Clustering motifs")

    linkage, clusters = cluster_motifs(similarity_matrix, args.threshold, args.clust_method)
    logger.stats("- Identified {0} clusters".format(len(clusters)))

    # Scaling for plots
    y_len = len(similarity_matrix.index.values)
    x_len = len(similarity_matrix.columns)

    y = args.height if args.height else y_len*scaling(y_len)
    x = args.width if args.width else x_len*scaling(x_len)

    cluster_f = out_prefix + "_" + "clusters.yml"
    dendrogram_f = out_prefix + "_" + "dendrogram"

    #Write out clusters and plot dendrogram
    write_yaml(clusters, cluster_f)
    plot_dendrogram(similarity_matrix.columns, linkage, 12, dendrogram_f, dendrogram_f, args.threshold, x, args.dpi, args.type)
    
    #IDEA: Plot dendrogram for each input file individually?
    """
    if args.merge or not args.motifs2:
        if not args.ncc or not args.nrc:
            if args.motifs2:
                filename_1 = os.path.splitext(os.path.basename(args.motifs1))[0] + "_" + os.path.splitext(os.path.basename(args.motifs2))[0]
                f_name_2 = filename_1

    if args.motifs2 and not args.merge:
        filename_2 = os.path.splitext(os.path.basename(args.motifs2))[0]
        f_name_2 = filename_2
        if not args.nrc: # if false skip writing yaml file and dendrogram for row clustering
            write_yaml(row_cluster, out_prefix + "_" + filename_2)
            plot_dendrogram(similarity_matrix.index.values, row_linkage, 12, args.outdir, args.prefix + "_" + filename_2, args.threshold, y, args.dpi, args.type)
        if not args.ncc:
            write_yaml(col_cluster, out_prefix + "_" + filename_1)
            plot_dendrogram(similarity_matrix.columns, col_linkage, 12, args.outdir, args.prefix + "_" + filename_1, args.threshold, y, args.dpi, args.type)        
    """

    #---------------------------------------- Consensus motif -----------------------------------------------#

    logger.info("Building consensus motifs for each cluster")

    cons = generate_consensus_motifs(motif_list, clusters, score_dict)

    # Image output path
    out_cons_img = os.path.join(args.outdir, "consensus_motifs_img")
    make_directory(out_cons_img)
    consensus_motif_out_wrapper(cons, out_prefix, out_cons_img, args.cons_format, args.prefix, args.type)

    #IDEA: Cluster and build consensus motif for each input file individually?
    """
    # Generate output depending on set parameters
    if args.merge or not args.motifs2: # col and row are identical
        if not args.ncc or not args.nrc:
            # Generate consensus motifs for each column cluster 
            cons = generate_consensus_motifs(motif_list, col_cluster, score_dict)
            # Save output
            consesus_motif_out_wrapper(cons, out_prefix, out_cons_img, args.cons_format, args.name, args.type)
    else: # col and row differ
        if not args.ncc:
            # Generate consensus motifs for each column cluster 
            col_cons = generate_consensus_motifs(motif_list, col_cluster, score_dict)
            # Save output
            consesus_motif_out_wrapper(col_cons, out_prefix, out_cons_img, args.cons_format, args.name, args.type, os.path.splitext(os.path.basename(args.motifs1))[0])
        if not args.nrc:
            # Generate consensus motifs for each row cluster 
            row_cons = generate_consensus_motifs(motif_list, row_cluster, score_dict)
            # Save output
            consesus_motif_out_wrapper(row_cons, out_prefix, out_cons_img, args.cons_format, args.name, args.type, os.path.splitext(os.path.basename(args.motifs2))[0])
    """

    #---------------------------------------- Plot heatmap --------------------------------------------------#

    logger.info("Plotting similarity heatmap")
    args.nrc = False
    args.ncc = False
    args.zscore = "None"

    pdf_out = out_prefix + "_heatmap_all." + args.type
    x_label = "All motifs"
    y_label = "All motifs"
    plot_heatmap(similarity_matrix, pdf_out, x, y, linkage, linkage, args.dpi, x_label, y_label, args.color, args.ncc, args.nrc, args.zscore)
    
    # Plot heatmaps for each combination of motif files
    comparisons = itertools.combinations(args.motifs, 2)
    for i, (motif_file_1, motif_file_2) in enumerate(comparisons):
        
        pdf_out = out_prefix + "_heatmap" + str(i) +"." + args.type
        logger.info("Plotting the heatmap to the file " + str(pdf_out))

        x_label, y_label = motif_file_1, motif_file_2

        #Create subset of matrices for row/col clustering
        motif_names_1 = [motif.name for motif in motif_dict[motif_file_1]]
        motif_names_2 = [motif.name for motif in motif_dict[motif_file_2]]

        m1_matrix, m2_matrix, similarity_matrix_sub = subset_matrix(similarity_matrix,  motif_names_1, motif_names_2)

        col_linkage = linkage(ssd.squareform(m1_matrix))
        row_linkage = linkage(ssd.squareform(m2_matrix))

        #Plot similarity heatmap between file1 and file2
        plot_heatmap(similarity_matrix_sub, pdf_out, x, y, col_linkage, row_linkage, args.dpi, x_label, y_label, args.color, args.ncc, args.nrc, args.zscore)
    
    # ClusterMotifs finished
    logger.end()


#--------------------------------------------------------------------------------------------------------#
if __name__ == "__main__":
    
    parser = argparse.ArgumentParser()
    parser = add_motifclust_arguments(parser)
    args = parser.parse_args()

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        sys.exit()

    run_motifclust(args)
